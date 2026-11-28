"""OPE-566 pilot signed-record verifier: trust chain, signer roles, customer anchors, time.

Every key here is generated at test time. No private key material is committed, and the
signing helpers below exist only in the test tree: the verifier module cannot sign.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import json
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from opentour_cell.pilot import signed_records as sr
from opentour_journal.crypto import P256_ORDER, canonical, der_to_raw, transcript

Json = dict[str, Any]
ENGAGEMENT = "eng-sentinel-0001"
REGISTRY_ID = "opentour-pilot-registry-sentinel"


def stamp(seconds: float) -> str:
    moment = datetime.now(UTC) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


@dataclass
class Signer:
    """Test-only issuer. Stands in for a hardware token that signs a SHA-256 digest."""

    role: str
    purposes: list[str] = field(default_factory=list)
    registry_revision: str = "rev-1"
    curve: ec.EllipticCurve = field(default_factory=ec.SECP256R1)

    def __post_init__(self) -> None:
        self.private = ec.generate_private_key(self.curve)
        self.key_id = f"{self.role.replace('_', '-')}-{uuid.uuid4().hex[:12]}"
        self.der = self.private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def public(self) -> Json:
        return {
            "key_id": self.key_id,
            "public_key_der": base64.b64encode(self.der).decode("ascii"),
            "public_key_sha256": hashlib.sha256(self.der).hexdigest(),
        }

    def entry(self, **changes: Any) -> Json:
        value: Json = {
            **self.public(),
            "registry_revision": self.registry_revision,
            "role": self.role,
            "purposes": list(self.purposes),
        }
        value.update(changes)
        return value

    def signature(self, payload: Json, purpose: str, **changes: Any) -> Json:
        digest = hashlib.sha256(transcript(purpose, payload)).digest()
        der = self.private.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        value: Json = {
            "profile_version": "1.0",
            "algorithm": "ES256",
            "encoding": "base64url_unpadded_r_s_64",
            "purpose": purpose,
            "signer_role": self.role,
            "key_id": self.key_id,
            "key_registry_revision": self.registry_revision,
            "payload_sha256": hashlib.sha256(canonical(payload)).hexdigest(),
            "transcript_sha256": digest.hex(),
            "signature": der_to_raw(der),
        }
        value.update(changes)
        return value


def envelope(payload: Json, purpose: str, *signers: Signer) -> Json:
    return {"payload": payload, "signatures": [s.signature(payload, purpose) for s in signers]}


def sign(payload: Json, purpose: str, *signers: Signer) -> bytes:
    return bytes(canonical(envelope(payload, purpose, *signers)))


def redigest(value: Json) -> bytes:
    """An attacker can recompute the unsigned digest fields; only the signature binds."""
    payload = value["payload"]
    for signature in value["signatures"]:
        signature["payload_sha256"] = hashlib.sha256(canonical(payload)).hexdigest()
        signature["transcript_sha256"] = hashlib.sha256(
            transcript(signature["purpose"], payload)
        ).hexdigest()
    return bytes(canonical(value))


def as_successor(signer: Signer, payload: Json, **changes: Any) -> Json:
    """A rotation signature in the role of the key being installed."""
    return signer.signature(
        payload, sr.ROTATION_PURPOSE, signer_role=sr.CUSTOMER_OWNER_SUCCESSOR, **changes
    )


# The literal string, so the tests pin it rather than echo the module's constant.
SCOPE_PURPOSE = "opentour.pilot.customer-consent-scope.v1"


def at(signer: Signer, revision: str) -> Signer:
    """The same customer key, signing at another anchor revision."""
    moved = copy.copy(signer)
    moved.registry_revision = revision
    return moved


def consent(**changes: Any) -> Json:
    value: Json = {
        "engagement_id": ENGAGEMENT,
        "organization_id": "org-sentinel-0001",
        "engagement_epoch": 3,
        "issued_at": stamp(-60),
        "not_after": stamp(3600),
    }
    value.update(changes)
    return value


@dataclass
class Kit:
    tmp: Path
    monkeypatch: pytest.MonkeyPatch
    root: Signer
    operator: Signer
    evaluator: Signer
    auditor: Signer
    customer: Signer
    worker: Path
    state: Path
    minimum: str = "1"

    def pin(self, **changes: Any) -> bytes:
        value: Json = {
            "profile_version": "1.0",
            "kind": "opentour.pilot.registry-root.v1",
            "registry_id": REGISTRY_ID,
            "minimum_registry_revision": self.minimum,
            "root_keys": [self.root.entry()],
        }
        value.update(changes)
        data = json.dumps(value, indent=2).encode()
        path = self.tmp / "kit" / "trust" / "pilot-registry-root.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.monkeypatch.setattr(sr, "ROOT_PIN_PATH", path)
        self.monkeypatch.setattr(sr, "APPROVED_ROOT_PIN_SHA256", hashlib.sha256(data).hexdigest())
        return data

    def issuers(self) -> list[Json]:
        return [self.operator.entry(), self.evaluator.entry(), self.auditor.entry()]

    def registry_payload(
        self, revision: str = "1", keys: list[Json] | None = None, **changes: Any
    ) -> Json:
        value: Json = {
            "profile_version": "1.0",
            "registry_id": REGISTRY_ID,
            "revision": revision,
            "issued_at": stamp(-60),
            "not_after": stamp(86400),
            "keys": self.issuers() if keys is None else keys,
        }
        value.update(changes)
        return value

    def registry(
        self, revision: str = "1", keys: list[Json] | None = None, **changes: Any
    ) -> bytes:
        payload = self.registry_payload(revision, keys, **changes)
        return sign(payload, sr.REGISTRY_PURPOSE, self.root)

    def verifier(self, registry: bytes | None = None) -> sr.PilotVerifier:
        return sr.PilotVerifier(self.registry() if registry is None else registry)

    def enrolment_payload(self, customer: Signer | None = None, **changes: Any) -> Json:
        payload: Json = {
            "profile_version": "1.0",
            "engagement_id": ENGAGEMENT,
            "anchor_revision": "1",
            "customer_key": (customer or self.customer).public(),
            # Purpose-scoped (OPE-566 68da165c): by default every current consent purpose,
            # so tests written before scoping keep their meaning.
            "consent_purposes": sorted(sr.CONSENT_PURPOSES),
            "issued_at": stamp(-60),
            "not_after": stamp(3600),
        }
        payload.update(changes)
        return payload

    def enrolment(self, customer: Signer | None = None, **changes: Any) -> bytes:
        payload = self.enrolment_payload(customer, **changes)
        return sign(payload, sr.ENROLMENT_PURPOSE, customer or self.customer, self.operator)

    def enrolled(self) -> sr.PilotVerifier:
        verifier = self.verifier()
        verifier.pin_customer_anchor(self.enrolment())
        return verifier

    def rotation_payload(self, previous: Signer, successor: Signer, **changes: Any) -> Json:
        payload: Json = {
            "profile_version": "1.0",
            "engagement_id": ENGAGEMENT,
            "previous_key_id": previous.key_id,
            "previous_anchor_revision": previous.registry_revision,
            "anchor_revision": successor.registry_revision,
            "customer_key": successor.public(),
            "issued_at": stamp(-60),
            "not_after": stamp(3600),
        }
        payload.update(changes)
        return payload

    def rotation(
        self, previous: Signer, successor: Signer, *signers: Signer, **changes: Any
    ) -> bytes:
        """Signed by the previous key and by the new key (proof of possession) by default."""
        payload = self.rotation_payload(previous, successor, **changes)
        if signers:
            return sign(payload, sr.ROTATION_PURPOSE, *signers)
        return canonical(
            {
                "payload": payload,
                "signatures": [
                    previous.signature(payload, sr.ROTATION_PURPOSE),
                    as_successor(successor, payload),
                ],
            }
        )

    def scope_payload(
        self,
        purposes: Any = None,
        *,
        customer: Signer | None = None,
        previous: str = "1",
        **changes: Any,
    ) -> Json:
        payload: Json = {
            "profile_version": "1.0",
            "engagement_id": ENGAGEMENT,
            "customer_key_id": (customer or self.customer).key_id,
            "previous_anchor_revision": previous,
            "anchor_revision": str(int(previous) + 1),
            "consent_purposes": sorted(sr.CONSENT_PURPOSES) if purposes is None else purposes,
            "issued_at": stamp(-60),
            "not_after": stamp(3600),
        }
        payload.update(changes)
        return payload

    def scope_update(
        self,
        purposes: Any = None,
        *signers: Signer,
        customer: Signer | None = None,
        previous: str = "1",
        **changes: Any,
    ) -> bytes:
        """Co-signed by the anchored customer key (at ``previous``) and the custodian."""
        payload = self.scope_payload(purposes, customer=customer, previous=previous, **changes)
        default = (at(customer or self.customer, previous), self.operator)
        return sign(payload, SCOPE_PURPOSE, *(signers or default))


@pytest.fixture
def kit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Kit:
    worker, state = tmp_path / "worker-volume", tmp_path / "trust-state"
    worker.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    value = Kit(
        tmp_path,
        monkeypatch,
        root=Signer(sr.OPERATOR_ROOT, [sr.REGISTRY_PURPOSE], registry_revision="root-1"),
        operator=Signer(
            sr.OPERATOR_CUSTODIAN,
            [
                sr.GRANT_PURPOSE,
                sr.SPEND_MANDATE_PURPOSE,
                sr.ROUTING_POLICY_PURPOSE,
                sr.WORKFLOW_INDEX_PUBLICATION_PURPOSE,
                sr.ENROLMENT_PURPOSE,
                SCOPE_PURPOSE,
            ],
        ),
        evaluator=Signer(sr.INDEPENDENT_EVALUATOR, [sr.VERDICT_PURPOSE]),
        auditor=Signer(sr.BENCHMARK_AUDITOR, [sr.BENCHMARK_AUDIT_PURPOSE]),
        customer=Signer(sr.CUSTOMER_OWNER, registry_revision="1"),
        worker=worker,
        state=state,
    )
    value.pin()
    # Production uses the worker-host location (OPE-577); tests use a temporary one.
    monkeypatch.setattr(sr, "ANTI_ROLLBACK_DIRECTORY", state)
    return value


def refused(code: str, action: Callable[[], object]) -> None:
    with pytest.raises(sr.PilotRecordRefused) as caught:
        action()
    assert caught.value.code == code


# --- Acceptance and per-purpose signer policy ----------------------------------------


def test_valid_records_accepted_with_their_required_signers(kit: Kit) -> None:
    verifier = kit.enrolled()
    for purpose in sorted(sr.CONSENT_PURPOSES):
        payload = consent(kind=purpose)
        record = sign(payload, purpose, kit.customer, kit.operator)
        assert verifier.verify(record, purpose) == payload
        swapped = sign(payload, purpose, kit.operator, kit.customer)
        assert verifier.verify(swapped, purpose) == payload
    for signer, purpose in (
        (kit.evaluator, sr.VERDICT_PURPOSE),
        (kit.auditor, sr.BENCHMARK_AUDIT_PURPOSE),
    ):
        payload = consent(kind=purpose)
        assert verifier.verify(sign(payload, purpose, signer), purpose) == payload


def test_policy_table_matches_the_owner_ruling() -> None:
    both = frozenset({sr.CUSTOMER_OWNER, sr.OPERATOR_CUSTODIAN})
    assert sr.REQUIRED_ROLES[sr.GRANT_PURPOSE] == both
    assert sr.REQUIRED_ROLES[sr.SPEND_MANDATE_PURPOSE] == both
    assert sr.REQUIRED_ROLES[sr.ROUTING_POLICY_PURPOSE] == both
    assert sr.REQUIRED_ROLES[sr.WORKFLOW_INDEX_PUBLICATION_PURPOSE] == both
    assert sr.REQUIRED_ROLES[sr.VERDICT_PURPOSE] == {sr.INDEPENDENT_EVALUATOR}
    assert sr.REQUIRED_ROLES[sr.BENCHMARK_AUDIT_PURPOSE] == {sr.BENCHMARK_AUDITOR}
    assert sr.REQUIRED_ROLES[sr.REGISTRY_PURPOSE] == {sr.OPERATOR_ROOT}
    # A rotation needs the previous and the new customer key; the operator does not co-sign.
    assert sr.REQUIRED_ROLES[sr.ROTATION_PURPOSE] == {
        sr.CUSTOMER_OWNER,
        sr.CUSTOMER_OWNER_SUCCESSOR,
    }
    assert sr.SPEND_MANDATE_PURPOSE == "opentour.pilot.spend-mandate.v1"


# --- OPE-547: workflow-index publication (customer-owner consent) ------------------------

PUBLICATION = "opentour.pilot.workflow-index-publication.v1"


def test_workflow_index_publication_is_registered_as_customer_consent_only() -> None:
    # The exact string the OPE-547 consumer verifies, and nothing broader.
    assert sr.WORKFLOW_INDEX_PUBLICATION_PURPOSE == PUBLICATION
    assert PUBLICATION in sr.CONSENT_PURPOSES
    assert PUBLICATION in sr.RECORD_PURPOSES
    assert sr.REQUIRED_ROLES[PUBLICATION] == {sr.CUSTOMER_OWNER, sr.OPERATOR_CUSTODIAN}
    holders = {role for role, purposes in sr.REGISTRY_ROLES.items() if PUBLICATION in purposes}
    assert holders == {sr.OPERATOR_CUSTODIAN}
    assert all(PUBLICATION not in purposes for purposes in sr.ROOT_ROLES.values())
    # Only this one purpose was added to the consent set in this registration.
    assert sr.CONSENT_PURPOSES == {
        sr.GRANT_PURPOSE,
        sr.SPEND_MANDATE_PURPOSE,
        sr.ROUTING_POLICY_PURPOSE,
        PUBLICATION,
    }
    assert sr.RECORD_PURPOSES == sr.CONSENT_PURPOSES | {
        sr.VERDICT_PURPOSE,
        sr.BENCHMARK_AUDIT_PURPOSE,
    }


def test_workflow_index_publication_accepted_with_anchored_customer_and_custodian(
    kit: Kit,
) -> None:
    verifier = kit.enrolled()
    payload = consent(tenant="tenant-sentinel-0001", workflows=[])
    record = sign(payload, PUBLICATION, kit.customer, kit.operator)
    assert verifier.verify(record, PUBLICATION) == payload


def test_workflow_index_publication_needs_an_anchored_customer(kit: Kit) -> None:
    # No enrolment pinned for the engagement: the operator cannot publish on its own say.
    verifier = kit.verifier()
    record = sign(consent(), PUBLICATION, kit.customer, kit.operator)
    refused("customer_anchor_missing", partial(verifier.verify, record, PUBLICATION))
    # Once anchored, a different customer-role key is still not the customer owner.
    verifier.pin_customer_anchor(kit.enrolment())
    stranger = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    forged = sign(consent(), PUBLICATION, stranger, kit.operator)
    refused("customer_key_not_anchored", partial(verifier.verify, forged, PUBLICATION))
    assert verifier.verify(record, PUBLICATION) == json.loads(record)["payload"]


def test_workflow_index_publication_custodian_must_be_enrolled_for_the_purpose(
    kit: Kit,
) -> None:
    # A current custodian without the publication purpose cannot co-sign it.
    narrow = Signer(sr.OPERATOR_CUSTODIAN, [sr.GRANT_PURPOSE])
    verifier = kit.verifier(kit.registry(keys=[*kit.issuers(), narrow.entry()]))
    verifier.pin_customer_anchor(kit.enrolment())
    record = sign(consent(), PUBLICATION, kit.customer, narrow)
    refused("key_purpose_not_permitted", partial(verifier.verify, record, PUBLICATION))
    # Signatures made for a grant are not publication consent.
    grant = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    refused("purpose_mismatch", partial(verifier.verify, grant, PUBLICATION))


@pytest.mark.parametrize("role", [sr.INDEPENDENT_EVALUATOR, sr.BENCHMARK_AUDITOR])
def test_workflow_index_publication_cannot_be_granted_to_other_registry_roles(
    kit: Kit, role: str
) -> None:
    signer = Signer(role, [PUBLICATION])
    refused(
        "trust_key_purposes_invalid",
        partial(kit.verifier, kit.registry(keys=[kit.operator.entry(), signer.entry()])),
    )


@pytest.mark.parametrize("purpose", sorted(sr.CONSENT_PURPOSES))
def test_consent_record_refused_with_either_signer_alone(kit: Kit, purpose: str) -> None:
    verifier = kit.enrolled()
    for signer in (kit.customer, kit.operator):
        record = sign(consent(), purpose, signer)
        refused("signature_role_missing", partial(verifier.verify, record, purpose))


def test_one_key_cannot_sign_twice_to_fake_two_roles(kit: Kit) -> None:
    verifier = kit.enrolled()
    payload = consent()
    # The operator key signs once as itself and once claiming the customer role.
    as_customer = kit.operator.signature(payload, sr.GRANT_PURPOSE, signer_role=sr.CUSTOMER_OWNER)
    doubled = {
        "payload": payload,
        "signatures": [kit.operator.signature(payload, sr.GRANT_PURPOSE), as_customer],
    }
    refused("signature_key_reused", partial(verifier.verify, canonical(doubled), sr.GRANT_PURPOSE))
    # The same role twice, even by two distinct operator keys, is not two consents.
    second = Signer(sr.OPERATOR_CUSTODIAN, [sr.GRANT_PURPOSE])
    record = sign(payload, sr.GRANT_PURPOSE, kit.operator, second)
    refused("signature_role_duplicate", partial(verifier.verify, record, sr.GRANT_PURPOSE))


def test_one_public_key_under_two_registry_ids_refused(kit: Kit) -> None:
    alias = kit.operator.entry(
        key_id="evaluator-alias-0001",
        role=sr.INDEPENDENT_EVALUATOR,
        purposes=[sr.VERDICT_PURPOSE],
    )
    refused(
        "trust_key_role_conflict",
        partial(kit.verifier, kit.registry(keys=[kit.operator.entry(), alias])),
    )
    same_role = kit.operator.entry(key_id="operator-alias-0001")
    refused(
        "trust_key_duplicate",
        partial(kit.verifier, kit.registry(keys=[kit.operator.entry(), same_role])),
    )


def test_evaluator_signature_on_a_grant_is_refused(kit: Kit) -> None:
    verifier = kit.enrolled()
    payload = consent()
    # Cryptographically valid: the evaluator really signed this grant transcript.
    claims_operator = kit.evaluator.signature(
        payload, sr.GRANT_PURPOSE, signer_role=sr.OPERATOR_CUSTODIAN
    )
    record = {
        "payload": payload,
        "signatures": [kit.customer.signature(payload, sr.GRANT_PURPOSE), claims_operator],
    }
    refused("key_role_not_permitted", partial(verifier.verify, canonical(record), sr.GRANT_PURPOSE))
    own_role = sign(payload, sr.GRANT_PURPOSE, kit.customer, kit.evaluator)
    refused("signature_role_unexpected", partial(verifier.verify, own_role, sr.GRANT_PURPOSE))
    claims_auditor = kit.auditor.signature(
        payload, sr.VERDICT_PURPOSE, signer_role=sr.INDEPENDENT_EVALUATOR
    )
    verdict = {"payload": payload, "signatures": [claims_auditor]}
    refused(
        "key_role_not_permitted", partial(verifier.verify, canonical(verdict), sr.VERDICT_PURPOSE)
    )


def test_key_limited_to_fewer_purposes_than_its_role(kit: Kit) -> None:
    narrow = Signer(sr.OPERATOR_CUSTODIAN, [sr.GRANT_PURPOSE])
    verifier = kit.verifier(kit.registry(keys=[*kit.issuers(), narrow.entry()]))
    verifier.pin_customer_anchor(kit.enrolment())
    grant = sign(consent(), sr.GRANT_PURPOSE, kit.customer, narrow)
    assert verifier.verify(grant, sr.GRANT_PURPOSE)
    policy = sign(consent(), sr.ROUTING_POLICY_PURPOSE, kit.customer, narrow)
    refused(
        "key_purpose_not_permitted", partial(verifier.verify, policy, sr.ROUTING_POLICY_PURPOSE)
    )


def test_same_signatures_refused_under_a_different_purpose(kit: Kit) -> None:
    verifier = kit.enrolled()
    record = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    # Both signers may sign spend mandates too, so only the transcript separates them.
    refused("purpose_mismatch", partial(verifier.verify, record, sr.SPEND_MANDATE_PURPOSE))
    relabelled = json.loads(record)
    for signature in relabelled["signatures"]:
        signature["purpose"] = sr.SPEND_MANDATE_PURPOSE
    refused(
        "transcript_digest_mismatch",
        partial(verifier.verify, canonical(relabelled), sr.SPEND_MANDATE_PURPOSE),
    )
    refused(
        "signature_invalid",
        partial(verifier.verify, redigest(relabelled), sr.SPEND_MANDATE_PURPOSE),
    )


def test_unknown_purpose_refused(kit: Kit) -> None:
    verifier = kit.enrolled()
    record = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    refused("purpose_unknown", partial(verifier.verify, record, "opentour.pilot.other.v1"))
    for internal in (sr.REGISTRY_PURPOSE, sr.ENROLMENT_PURPOSE, sr.ROTATION_PURPOSE):
        refused("purpose_unknown", partial(verifier.verify, kit.registry(), internal))


def test_unknown_key_id_refused(kit: Kit) -> None:
    verifier = kit.verifier()
    stranger = Signer(sr.INDEPENDENT_EVALUATOR, [sr.VERDICT_PURPOSE])
    record = sign(consent(), sr.VERDICT_PURPOSE, stranger)
    refused("key_unknown", partial(verifier.verify, record, sr.VERDICT_PURPOSE))


def test_unknown_registry_revision_refused(kit: Kit) -> None:
    verifier = kit.verifier()
    payload = consent()
    stale = kit.evaluator.signature(payload, sr.VERDICT_PURPOSE, key_registry_revision="rev-2")
    record = canonical({"payload": payload, "signatures": [stale]})
    refused("key_revision_not_active", partial(verifier.verify, record, sr.VERDICT_PURPOSE))


def test_record_by_key_absent_from_verified_registry_refused(kit: Kit) -> None:
    # Revocation: a newer registry revision that drops the evaluator key.
    record = sign(consent(), sr.VERDICT_PURPOSE, kit.evaluator)
    assert kit.verifier().verify(record, sr.VERDICT_PURPOSE)
    revoked = kit.registry("2", keys=[kit.operator.entry(), kit.auditor.entry()])
    verifier = kit.verifier(revoked)
    refused("key_unknown", partial(verifier.verify, record, sr.VERDICT_PURPOSE))


def test_root_key_cannot_sign_records(kit: Kit) -> None:
    verifier = kit.verifier()
    payload = consent()
    claims = kit.root.signature(payload, sr.VERDICT_PURPOSE, signer_role=sr.INDEPENDENT_EVALUATOR)
    record = canonical({"payload": payload, "signatures": [claims]})
    refused("key_unknown", partial(verifier.verify, record, sr.VERDICT_PURPOSE))


# --- Customer anchors ----------------------------------------------------------------


def test_consent_record_needs_a_pinned_anchor(kit: Kit) -> None:
    verifier = kit.verifier()
    record = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    refused("customer_anchor_missing", partial(verifier.verify, record, sr.GRANT_PURPOSE))
    no_engagement = consent()
    del no_engagement["engagement_id"]
    bare = sign(no_engagement, sr.GRANT_PURPOSE, kit.customer, kit.operator)
    refused("record_schema_invalid", partial(verifier.verify, bare, sr.GRANT_PURPOSE))


def test_enrolment_requires_customer_and_operator_signatures(kit: Kit) -> None:
    verifier = kit.verifier()
    payload = kit.enrolment_payload()
    for signers in ((kit.customer,), (kit.operator,)):
        record = sign(payload, sr.ENROLMENT_PURPOSE, *signers)
        refused("signature_role_missing", partial(verifier.pin_customer_anchor, record))
    assert not record_path(kit, ANCHOR_KEY).exists()


def test_enrolment_proves_possession_and_pins_idempotently(kit: Kit) -> None:
    verifier = kit.verifier()
    # The enrolled key itself must sign: another key cannot enrol it on its behalf.
    impostor = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    unproven = sign(
        kit.enrolment_payload(impostor), sr.ENROLMENT_PURPOSE, kit.customer, kit.operator
    )
    refused("enrolment_key_mismatch", partial(verifier.pin_customer_anchor, unproven))
    enrolment = kit.enrolment()
    anchor = verifier.pin_customer_anchor(enrolment)
    assert (anchor.key_id, anchor.anchor_revision) == (kit.customer.key_id, 1)
    assert verifier.pin_customer_anchor(enrolment) == anchor


def test_operator_cannot_re_enrol_a_different_customer_key(kit: Kit) -> None:
    verifier = kit.enrolled()
    substitute = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    refused(
        "customer_anchor_already_pinned",
        partial(verifier.pin_customer_anchor, kit.enrolment(substitute)),
    )
    forged = sign(consent(), sr.GRANT_PURPOSE, substitute, kit.operator)
    refused("customer_key_not_anchored", partial(verifier.verify, forged, sr.GRANT_PURPOSE))


def test_enrolled_key_must_be_distinct_from_operator_registry_keys(kit: Kit) -> None:
    verifier = kit.verifier()
    own = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    own.private, own.der = kit.operator.private, kit.operator.der
    refused("customer_key_not_distinct", partial(verifier.pin_customer_anchor, kit.enrolment(own)))
    reused_id = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    reused_id.key_id = kit.operator.key_id
    refused(
        "customer_key_not_distinct",
        partial(verifier.pin_customer_anchor, kit.enrolment(reused_id)),
    )
    refused(
        "customer_anchor_revision_invalid",
        partial(verifier.pin_customer_anchor, kit.enrolment(anchor_revision="2")),
    )


def test_operator_signed_registry_revision_cannot_change_a_customer_anchor(kit: Kit) -> None:
    """The point of the second-round ruling: the operator alone cannot replace the customer."""
    verifier = kit.enrolled()
    genuine = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    assert verifier.verify(genuine, sr.GRANT_PURPOSE)
    substitute = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    # 1. A registry may not carry customer-owner keys at all, however validly root-signed.
    for purposes in ([], [sr.GRANT_PURPOSE]):
        with_customer = kit.registry(
            "2", keys=[*kit.issuers(), substitute.entry(purposes=purposes)]
        )
        refused("trust_key_role_invalid", partial(kit.verifier, with_customer))
    # 2. A valid newer registry that adds operator keys leaves the anchor exactly as it was.
    extra = Signer(sr.OPERATOR_CUSTODIAN, [sr.GRANT_PURPOSE])
    newer = kit.verifier(kit.registry("2", keys=[*kit.issuers(), extra.entry()]))
    assert newer.registry_revision == 2
    assert newer.verify(genuine, sr.GRANT_PURPOSE)
    # 3. A registry key presented as the customer is not the anchored customer key.
    payload = consent()
    as_customer = extra.signature(payload, sr.GRANT_PURPOSE, signer_role=sr.CUSTOMER_OWNER)
    minted = {
        "payload": payload,
        "signatures": [as_customer, kit.operator.signature(payload, sr.GRANT_PURPOSE)],
    }
    refused("customer_key_not_anchored", partial(newer.verify, canonical(minted), sr.GRANT_PURPOSE))
    forged = sign(consent(), sr.GRANT_PURPOSE, substitute, kit.operator)
    refused("customer_key_not_anchored", partial(newer.verify, forged, sr.GRANT_PURPOSE))
    # 4. Nor can the operator re-enrol, or rotate without the previous customer key.
    refused(
        "customer_anchor_already_pinned",
        partial(newer.pin_customer_anchor, kit.enrolment(substitute)),
    )
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    operator_rotation = kit.rotation(kit.customer, successor, kit.operator)
    refused("signature_role_unexpected", partial(newer.rotate_customer_key, operator_rotation))
    assert newer.verify(genuine, sr.GRANT_PURPOSE)


def test_rotation_signed_by_previous_and_new_key_replaces_the_anchor(kit: Kit) -> None:
    verifier = kit.enrolled()
    old_record = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    anchor = verifier.rotate_customer_key(kit.rotation(kit.customer, successor))
    assert (anchor.key_id, anchor.anchor_revision) == (successor.key_id, 2)
    refused("customer_key_not_anchored", partial(verifier.verify, old_record, sr.GRANT_PURPOSE))
    new_record = sign(consent(), sr.GRANT_PURPOSE, successor, kit.operator)
    assert verifier.verify(new_record, sr.GRANT_PURPOSE)


def test_rotation_signed_only_by_the_previous_key_is_refused(kit: Kit) -> None:
    verifier = kit.enrolled()
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    previous_only = kit.rotation(kit.customer, successor, kit.customer)
    refused("signature_role_missing", partial(verifier.rotate_customer_key, previous_only))
    genuine = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    assert verifier.verify(genuine, sr.GRANT_PURPOSE)


def test_rotation_new_key_signature_must_be_by_the_installed_key(kit: Kit) -> None:
    verifier = kit.enrolled()
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    third = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    payload = kit.rotation_payload(kit.customer, successor)
    previous = kit.customer.signature(payload, sr.ROTATION_PURPOSE)
    # A third key signs as the new key: it is not the key being installed.
    by_third = canonical(
        {"payload": payload, "signatures": [previous, as_successor(third, payload)]}
    )
    refused("rotation_successor_key_mismatch", partial(verifier.rotate_customer_key, by_third))
    # A third key presenting the installed key's id still cannot produce its signature.
    impersonated = as_successor(third, payload, key_id=successor.key_id)
    forged = canonical({"payload": payload, "signatures": [previous, impersonated]})
    refused("signature_invalid", partial(verifier.rotate_customer_key, forged))
    # The new key's signature at the wrong anchor revision is not the installed key either.
    stale = as_successor(successor, payload, key_registry_revision="1")
    wrong_revision = canonical({"payload": payload, "signatures": [previous, stale]})
    refused(
        "rotation_successor_key_mismatch",
        partial(verifier.rotate_customer_key, wrong_revision),
    )
    genuine = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    assert verifier.verify(genuine, sr.GRANT_PURPOSE)
    assert verifier.rotate_customer_key(kit.rotation(kit.customer, successor)).anchor_revision == 2


def test_rotation_not_signed_by_previous_key_refused(kit: Kit) -> None:
    verifier = kit.enrolled()
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    payload = kit.rotation_payload(kit.customer, successor)
    new_key = as_successor(successor, payload)
    # Signed only by the new key.
    alone = canonical({"payload": payload, "signatures": [new_key]})
    refused("signature_role_missing", partial(verifier.rotate_customer_key, alone))
    # A third key, or a registry key, standing in for the previous customer key.
    third = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    for stand_in in (third, kit.operator):
        claims = stand_in.signature(
            payload, sr.ROTATION_PURPOSE, signer_role=sr.CUSTOMER_OWNER, key_registry_revision="1"
        )
        record = canonical({"payload": payload, "signatures": [claims, new_key]})
        refused("customer_key_not_anchored", partial(verifier.rotate_customer_key, record))
    genuine = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    assert verifier.verify(genuine, sr.GRANT_PURPOSE)


def test_rotation_must_be_the_exact_successor(kit: Kit) -> None:
    verifier = kit.enrolled()
    skip = Signer(sr.CUSTOMER_OWNER, registry_revision="3")
    refused(
        "customer_rotation_not_successor",
        partial(verifier.rotate_customer_key, kit.rotation(kit.customer, skip)),
    )
    second = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    first_rotation = kit.rotation(kit.customer, second)
    verifier.rotate_customer_key(first_rotation)
    third = Signer(sr.CUSTOMER_OWNER, registry_revision="3")
    verifier.rotate_customer_key(kit.rotation(second, third))
    # Replaying the first rotation cannot bring back the second key.
    refused("customer_key_not_anchored", partial(verifier.rotate_customer_key, first_rotation))
    # Rotating onto the same key under a new id: one key cannot sign as both.
    same = Signer(sr.CUSTOMER_OWNER, registry_revision="4")
    same.private, same.der = third.private, third.der
    refused(
        "signature_key_reused",
        partial(verifier.rotate_customer_key, kit.rotation(third, same)),
    )
    # Rotating onto an operator registry key, even with possession of it, is refused.
    operator_key = Signer(sr.CUSTOMER_OWNER, registry_revision="4")
    operator_key.private, operator_key.der = kit.operator.private, kit.operator.der
    refused(
        "customer_key_not_distinct",
        partial(verifier.rotate_customer_key, kit.rotation(third, operator_key)),
    )


# --- Anti-rollback trust-state store -------------------------------------------------


def record_path(kit: Kit, key: str) -> Path:
    return kit.state / sr.RECORDS_NAME / (hashlib.sha256(key.encode()).hexdigest() + ".json")


def stored(kit: Kit, key: str) -> Json:
    value: Json = json.loads(record_path(kit, key).read_bytes())
    return value


ANCHOR_KEY = "customer_anchor:" + ENGAGEMENT
REVISION_KEY = "customer_anchor_revision:" + ENGAGEMENT


def accept_any(current: sr.MonotonicRecord | None, proposed: sr.MonotonicRecord) -> None:
    return None


def test_store_location_is_fixed_configuration(kit: Kit, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sr, "ANTI_ROLLBACK_DIRECTORY", None)
    refused("anti_rollback_location_not_configured", kit.verifier)
    refused("anti_rollback_location_not_configured", sr.TrustStateStore)
    for location in (Path("relative/trust-state"), kit.tmp / "absent"):
        monkeypatch.setattr(sr, "ANTI_ROLLBACK_DIRECTORY", location)
        refused("anti_rollback_state_unavailable", kit.verifier)
    link = kit.tmp / "linked-state"
    link.symlink_to(kit.state, target_is_directory=True)
    monkeypatch.setattr(sr, "ANTI_ROLLBACK_DIRECTORY", link)
    refused("anti_rollback_state_unavailable", kit.verifier)


def test_shipped_store_location_is_the_worker_host_antirollback_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()
    assert sr.ANTI_ROLLBACK_DIRECTORY == Path("/var/lib/opentour-antirollback")


def test_store_admits_new_namespaces_without_a_format_change(kit: Kit) -> None:
    store = sr.TrustStateStore()
    with store.locked():
        epoch = sr.MonotonicRecord("revocation_epoch:org-sentinel-0001", 4, {})
        assert store.advance(epoch, accept_any)
        assert not store.advance(epoch, accept_any)
        withdrawn = sr.MonotonicRecord(
            "withdrawn_mandate:org-sentinel-0001:mandate-0001", 1, {"withdrawn": True}
        )
        assert store.advance(withdrawn, accept_any)
        # The generic rule refuses any backwards move, whatever the namespace rule allows.
        lower = sr.MonotonicRecord("revocation_epoch:org-sentinel-0001", 3, {})
        refused("trust_state_regressed", partial(store.advance, lower, accept_any))
        changed = sr.MonotonicRecord("revocation_epoch:org-sentinel-0001", 4, {"x": 1})
        refused("trust_state_conflict", partial(store.advance, changed, accept_any))
        for key in ("Bad", "registry revision", "", "ns:" + "x" * 513, "ns:\n"):
            refused(
                "trust_state_key_invalid",
                partial(store.advance, sr.MonotonicRecord(key, 1, {}), accept_any),
            )
    assert stored(kit, "revocation_epoch:org-sentinel-0001")["sequence"] == "4"
    record = store.read("withdrawn_mandate:org-sentinel-0001:mandate-0001")
    assert record == sr.MonotonicRecord(
        "withdrawn_mandate:org-sentinel-0001:mandate-0001", 1, {"withdrawn": True}
    )


def test_store_state_is_independent_of_a_restored_data_directory(kit: Kit) -> None:
    """Simulated: a data-directory copy and restore leaves the store untouched.

    Restore exclusion end to end depends on the OPE-577 snapshot boundary and is not
    demonstrated here.
    """
    (kit.worker / "managed-runs.sqlite3").write_bytes(b"sentinel-ledger-v1")
    old_registry = kit.registry("1")
    old_record = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    kit.verifier(old_registry).pin_customer_anchor(kit.enrolment())
    snapshot = kit.tmp / "snapshot"
    shutil.copytree(kit.worker, snapshot)
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    newer_registry = kit.registry("2", keys=[kit.operator.entry(), kit.auditor.entry()])
    kit.verifier(newer_registry).rotate_customer_key(kit.rotation(kit.customer, successor))
    (kit.worker / "managed-runs.sqlite3").write_bytes(b"sentinel-ledger-v2")
    shutil.rmtree(kit.worker)
    shutil.copytree(snapshot, kit.worker)
    assert (kit.worker / "managed-runs.sqlite3").read_bytes() == b"sentinel-ledger-v1"
    refused("registry_revision_rolled_back", partial(kit.verifier, old_registry))
    restored = kit.verifier(newer_registry)
    refused("customer_key_not_anchored", partial(restored.verify, old_record, sr.GRANT_PURPOSE))
    refused(
        "customer_anchor_already_pinned",
        partial(restored.pin_customer_anchor, kit.enrolment()),
    )
    current = sign(consent(), sr.GRANT_PURPOSE, successor, kit.operator)
    assert restored.verify(current, sr.GRANT_PURPOSE)


def test_older_registry_revision_cannot_resurrect_a_dropped_key(kit: Kit) -> None:
    old = kit.registry("1")
    record = sign(consent(), sr.VERDICT_PURPOSE, kit.evaluator)
    assert kit.verifier(old).verify(record, sr.VERDICT_PURPOSE)
    kit.verifier(kit.registry("2", keys=[kit.operator.entry(), kit.auditor.entry()]))
    # The old registry is still validly signed and unexpired; the floor refuses it.
    refused("registry_revision_rolled_back", partial(kit.verifier, old))
    assert stored(kit, "registry_revision")["sequence"] == "2"


def test_same_revision_with_different_contents_is_equivocation(kit: Kit) -> None:
    first = kit.registry("3")
    kit.verifier(first)
    assert kit.verifier(first).registry_revision == 3
    other = kit.registry("3", keys=[kit.evaluator.entry()])
    refused("registry_revision_equivocated", partial(kit.verifier, other))


def test_registry_below_kit_minimum_refused_without_local_state(kit: Kit) -> None:
    kit.minimum = "5"
    kit.pin()
    refused("registry_revision_below_kit_minimum", partial(kit.verifier, kit.registry("4")))
    assert not record_path(kit, "registry_revision").exists()
    assert kit.verifier(kit.registry("5")).registry_revision == 5


def test_unreadable_state_fails_closed(kit: Kit) -> None:
    verifier = kit.enrolled()
    record = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    anchor_file = record_path(kit, ANCHOR_KEY)
    good = stored(kit, ANCHOR_KEY)
    for broken in (
        b'{"key":"' + ANCHOR_KEY.encode() + b'"}',
        canonical({**good, "format": "opentour.pilot.trust-state.v0"}),
        canonical({**good, "key": "customer_anchor:eng-other"}),
        canonical({**good, "extra": 1}),
        json.dumps(good, indent=1).encode(),
        canonical({**good, "value": {"record_sha256": "0" * 64}}),
    ):
        anchor_file.write_bytes(broken)
        refused("trust_state_unreadable", partial(verifier.verify, record, sr.GRANT_PURPOSE))
    floor_file = record_path(kit, "registry_revision")
    floor_file.write_bytes(b'{"sequence":"0"}')
    refused("trust_state_unreadable", kit.verifier)


def test_refused_registry_does_not_advance_floor(kit: Kit) -> None:
    kit.verifier(kit.registry("1"))
    bad = kit.registry("9", keys=[kit.operator.entry(purposes=[])])
    refused("trust_key_purposes_invalid", partial(kit.verifier, bad))
    assert stored(kit, "registry_revision")["sequence"] == "1"


def test_store_record_format_is_strict_and_versioned(kit: Kit) -> None:
    registry = kit.registry()
    enrolment = kit.enrolment()
    kit.verifier(registry).pin_customer_anchor(enrolment)
    registry_payload = json.loads(registry)["payload"]
    assert stored(kit, "registry_revision") == {
        "format": "opentour.pilot.trust-state.v1",
        "key": "registry_revision",
        "sequence": "1",
        "value": {
            "registry_payload_sha256": hashlib.sha256(canonical(registry_payload)).hexdigest()
        },
    }
    # The anchor is the signed evidence chain, never a bare key field.
    assert stored(kit, ANCHOR_KEY) == {
        "format": "opentour.pilot.trust-state.v1",
        "key": ANCHOR_KEY,
        "sequence": "1",
        "value": {"chain": [json.loads(enrolment)]},
    }
    enrolment_payload = json.loads(enrolment)["payload"]
    assert stored(kit, REVISION_KEY) == {
        "format": "opentour.pilot.trust-state.v1",
        "key": REVISION_KEY,
        "sequence": "1",
        "value": {"head_payload_sha256": hashlib.sha256(canonical(enrolment_payload)).hexdigest()},
    }
    for key in ("registry_revision", ANCHOR_KEY, REVISION_KEY):
        assert canonical(stored(kit, key)) == record_path(kit, key).read_bytes()


def test_store_anchor_rules_require_the_immediate_successor(kit: Kit) -> None:
    kit.enrolled()
    store = sr.TrustStateStore()
    value = {"chain": [{"x": 1}]}
    with store.locked():
        for sequence in (3, 4):
            refused(
                "customer_rotation_not_successor",
                partial(
                    store.advance, sr.MonotonicRecord(ANCHOR_KEY, sequence, value), sr._chain_rule
                ),
            )
        refused(
            "customer_rotation_not_successor",
            partial(
                store.advance,
                sr.MonotonicRecord("customer_anchor:eng-other", 2, value),
                sr._chain_rule,
            ),
        )
        refused(
            "customer_anchor_rolled_back",
            partial(
                store.advance,
                sr.MonotonicRecord(REVISION_KEY, 0, {"head_payload_sha256": "0" * 64}),
                sr._forward_rule,
            ),
        )
        refused(
            "registry_revision_rolled_back",
            partial(
                store.advance,
                sr.MonotonicRecord("registry_revision", 0, {"registry_payload_sha256": "0" * 64}),
                sr._registry_rule,
            ),
        )
    assert stored(kit, ANCHOR_KEY)["sequence"] == "1"


# --- Review findings (PR #209) -------------------------------------------------------


def test_f1_one_key_cannot_hold_two_roles_across_registry_revisions(kit: Kit) -> None:
    # Reviewer's repro: the custodian key re-registered at another revision as evaluator.
    as_evaluator = kit.operator.entry(
        registry_revision="rev-2", role=sr.INDEPENDENT_EVALUATOR, purposes=[sr.VERDICT_PURPOSE]
    )
    keys = [kit.operator.entry(), as_evaluator, kit.auditor.entry()]
    refused("trust_key_role_conflict", partial(kit.verifier, kit.registry(keys=keys)))
    # The same key id under another role, even with a different public key.
    by_id = kit.evaluator.entry(key_id=kit.operator.key_id, registry_revision="rev-2")
    refused(
        "trust_key_role_conflict",
        partial(kit.verifier, kit.registry(keys=[kit.operator.entry(), by_id])),
    )
    # The same key and role at two registry revisions remains allowed (rotation overlap).
    same_role = kit.operator.entry(registry_revision="rev-2")
    verifier = kit.verifier(kit.registry(keys=[kit.operator.entry(), same_role]))
    assert verifier.registry_revision == 1


def test_f2_a_verifier_built_from_an_older_registry_sees_the_revocation(kit: Kit) -> None:
    verdict = sign(consent(), sr.VERDICT_PURPOSE, kit.evaluator)
    old = kit.verifier(kit.registry("1"))
    assert old.verify(verdict, sr.VERDICT_PURPOSE)
    kit.verifier(kit.registry("2", keys=[kit.operator.entry(), kit.auditor.entry()]))
    refused("registry_superseded", partial(old.verify, verdict, sr.VERDICT_PURPOSE))
    refused("registry_superseded", partial(old.pin_customer_anchor, kit.enrolment()))
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    refused(
        "registry_superseded",
        partial(old.rotate_customer_key, kit.rotation(kit.customer, successor)),
    )


def test_f2_a_missing_registry_floor_supersedes_every_verifier(kit: Kit) -> None:
    verifier = kit.verifier()
    verdict = sign(consent(), sr.VERDICT_PURPOSE, kit.evaluator)
    record_path(kit, "registry_revision").unlink()
    refused("registry_superseded", partial(verifier.verify, verdict, sr.VERDICT_PURPOSE))


def test_f3_an_overwritten_anchor_is_refused(kit: Kit) -> None:
    verifier = kit.enrolled()
    grant = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    anchor_file = record_path(kit, ANCHOR_KEY)
    genuine = stored(kit, ANCHOR_KEY)
    attacker = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    forged_grant = sign(consent(), sr.GRANT_PURPOSE, attacker, kit.operator)
    # 1. Any key with a zero digest, in the old bare-key shape.
    bare = {**genuine, "value": {"customer_key": attacker.public(), "record_sha256": "0" * 64}}
    anchor_file.write_bytes(canonical(bare))
    refused("trust_state_unreadable", partial(verifier.verify, forged_grant, sr.GRANT_PURPOSE))
    # 2. A chain whose enrolment lacks a registry custodian signature.
    stranger = Signer(sr.OPERATOR_CUSTODIAN, [sr.ENROLMENT_PURPOSE])
    fake = sign(kit.enrolment_payload(attacker), sr.ENROLMENT_PURPOSE, attacker, stranger)
    anchor_file.write_bytes(canonical({**genuine, "value": {"chain": [json.loads(fake)]}}))
    refused(
        "customer_anchor_unverifiable", partial(verifier.verify, forged_grant, sr.GRANT_PURPOSE)
    )
    # 3. A genuine enrolment with its customer key field swapped after signing.
    edited = json.loads(json.dumps(genuine))
    edited["value"]["chain"][0]["payload"]["customer_key"] = attacker.public()
    anchor_file.write_bytes(canonical(edited))
    refused(
        "customer_anchor_unverifiable", partial(verifier.verify, forged_grant, sr.GRANT_PURPOSE)
    )
    # 4. A validly co-signed but different enrolment, against the stored revision floor.
    other = kit.enrolment(attacker)
    anchor_file.write_bytes(canonical({**genuine, "value": {"chain": [json.loads(other)]}}))
    refused(
        "customer_anchor_inconsistent", partial(verifier.verify, forged_grant, sr.GRANT_PURPOSE)
    )
    anchor_file.write_bytes(canonical(genuine))
    assert verifier.verify(grant, sr.GRANT_PURPOSE)


def test_f3_deleting_the_anchor_does_not_allow_re_enrolment(kit: Kit) -> None:
    verifier = kit.verifier()
    enrolment = kit.enrolment()
    verifier.pin_customer_anchor(enrolment)
    record_path(kit, ANCHOR_KEY).unlink()
    grant = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    refused("customer_anchor_inconsistent", partial(verifier.verify, grant, sr.GRANT_PURPOSE))
    held = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    refused(
        "customer_anchor_already_pinned", partial(verifier.pin_customer_anchor, kit.enrolment(held))
    )
    # Re-presenting the genuine enrolment restores the genuine anchor only.
    verifier.pin_customer_anchor(enrolment)
    assert verifier.verify(grant, sr.GRANT_PURPOSE)


def test_f3_restoring_an_older_anchor_copy_after_rotation_is_refused(kit: Kit) -> None:
    verifier = kit.enrolled()
    old_copy = record_path(kit, ANCHOR_KEY).read_bytes()
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    verifier.rotate_customer_key(kit.rotation(kit.customer, successor))
    record_path(kit, ANCHOR_KEY).write_bytes(old_copy)
    old_grant = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    refused("customer_anchor_rolled_back", partial(verifier.verify, old_grant, sr.GRANT_PURPOSE))
    refused(
        "customer_anchor_rolled_back",
        partial(verifier.rotate_customer_key, kit.rotation(kit.customer, successor)),
    )


def test_f3_a_chain_ahead_of_its_floor_still_verifies(kit: Kit) -> None:
    # An interrupted rotation: the signed chain was written, the floor was not.
    verifier = kit.enrolled()
    old_floor = record_path(kit, REVISION_KEY).read_bytes()
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    verifier.rotate_customer_key(kit.rotation(kit.customer, successor))
    record_path(kit, REVISION_KEY).write_bytes(old_floor)
    grant = sign(consent(), sr.GRANT_PURPOSE, successor, kit.operator)
    assert verifier.verify(grant, sr.GRANT_PURPOSE)
    old_grant = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    refused("customer_key_not_anchored", partial(verifier.verify, old_grant, sr.GRANT_PURPOSE))


def test_f3_documented_limit_custodian_key_holder_can_re_enrol_after_deleting_both(
    kit: Kit,
) -> None:
    """Not a defence: stated in the module docstring. OPE-583 covers it out of band."""
    verifier = kit.enrolled()
    record_path(kit, ANCHOR_KEY).unlink()
    record_path(kit, REVISION_KEY).unlink()
    held = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    assert verifier.pin_customer_anchor(kit.enrolment(held)).key_id == held.key_id


def test_f4_a_re_signed_registry_is_the_same_registry(kit: Kit) -> None:
    payload = kit.registry_payload("1")
    resigned = sign(payload, sr.REGISTRY_PURPOSE, kit.root)  # randomized ECDSA nonce
    genuine = sign(payload, sr.REGISTRY_PURPOSE, kit.root)
    assert resigned != genuine
    kit.verifier(resigned)
    assert kit.verifier(genuine).registry_revision == 1
    refused(
        "registry_revision_equivocated",
        partial(kit.verifier, kit.registry("1", keys=[kit.evaluator.entry()])),
    )


def test_f4_a_high_s_registry_copy_does_not_lock_out_the_genuine_one(kit: Kit) -> None:
    genuine = kit.registry("1")
    value = json.loads(genuine)
    raw = base64.urlsafe_b64decode(value["signatures"][0]["signature"] + "==")
    r, s = raw[:32], int.from_bytes(raw[32:], "big")
    high_s = r + (P256_ORDER - s).to_bytes(32, "big")
    value["signatures"][0]["signature"] = base64.urlsafe_b64encode(high_s).rstrip(b"=").decode()
    flipped = canonical(value)
    assert flipped != genuine
    # Not normalised here (an OPE-217 decision): both encodings verify, as one registry.
    kit.verifier(flipped)
    assert kit.verifier(genuine).registry_revision == 1


def test_f4_enrolment_idempotency_ignores_signature_order(kit: Kit) -> None:
    verifier = kit.verifier()
    payload = kit.enrolment_payload()
    forward = sign(payload, sr.ENROLMENT_PURPOSE, kit.customer, kit.operator)
    reversed_order = sign(payload, sr.ENROLMENT_PURPOSE, kit.operator, kit.customer)
    first = verifier.pin_customer_anchor(reversed_order)
    assert verifier.pin_customer_anchor(forward) == first


def test_f5_the_root_key_is_never_a_registry_or_customer_key(kit: Kit) -> None:
    as_custodian = kit.root.entry(
        key_id="operator-custodian-root", role=sr.OPERATOR_CUSTODIAN, purposes=[sr.GRANT_PURPOSE]
    )
    refused("trust_key_role_conflict", partial(kit.verifier, kit.registry(keys=[as_custodian])))
    same_id = kit.operator.entry(key_id=kit.root.key_id, registry_revision="rev-9")
    refused("trust_key_role_conflict", partial(kit.verifier, kit.registry(keys=[same_id])))
    verifier = kit.verifier()
    root_as_customer = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    root_as_customer.private, root_as_customer.der = kit.root.private, kit.root.der
    refused(
        "customer_key_not_distinct",
        partial(verifier.pin_customer_anchor, kit.enrolment(root_as_customer)),
    )
    verifier.pin_customer_anchor(kit.enrolment())
    root_successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    root_successor.private, root_successor.der = kit.root.private, kit.root.der
    refused(
        "customer_key_not_distinct",
        partial(verifier.rotate_customer_key, kit.rotation(kit.customer, root_successor)),
    )


def test_f6_validity_window_ceiling(kit: Kit) -> None:
    verifier = kit.verifier()
    long = consent(issued_at=stamp(-60), not_after=stamp(367 * 86400))
    refused(
        "record_validity_window_too_long",
        partial(verifier.verify, sign(long, sr.VERDICT_PURPOSE, kit.evaluator), sr.VERDICT_PURPOSE),
    )
    within = consent(issued_at=stamp(-60), not_after=stamp(365 * 86400))
    assert verifier.verify(sign(within, sr.VERDICT_PURPOSE, kit.evaluator), sr.VERDICT_PURPOSE)
    refused(
        "registry_validity_window_too_long",
        partial(kit.verifier, kit.registry("2", not_after=stamp(400 * 86400))),
    )


def test_f6_store_writes_every_byte_on_short_writes(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = kit.tmp / "short-write"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    real_write = os.write
    body = b"x" * 5000
    with monkeypatch.context() as patch:
        patch.setattr(os, "write", lambda f, data: real_write(f, bytes(data[:7])))
        sr._write_all(fd, body)
    os.close(fd)
    assert path.read_bytes() == body


def test_f6_store_refuses_symlinked_records_and_shared_directories(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = kit.enrolled()
    grant = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    anchor_file = record_path(kit, ANCHOR_KEY)
    elsewhere = kit.tmp / "elsewhere.json"
    elsewhere.write_bytes(anchor_file.read_bytes())
    anchor_file.unlink()
    anchor_file.symlink_to(elsewhere)
    refused("trust_state_unreadable", partial(verifier.verify, grant, sr.GRANT_PURPOSE))
    kit.state.chmod(0o770)
    refused("anti_rollback_state_unavailable", kit.verifier)
    kit.state.chmod(0o700)
    (kit.state / sr.RECORDS_NAME).chmod(0o777)
    refused("anti_rollback_state_unavailable", partial(kit.verifier, kit.registry("2")))
    (kit.state / sr.RECORDS_NAME).chmod(0o700)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    refused("anti_rollback_state_unavailable", kit.verifier)


def test_f6_advance_requires_the_lock(kit: Kit) -> None:
    store = sr.TrustStateStore()
    record = sr.MonotonicRecord("revocation_epoch:org-sentinel-0001", 1, {})
    refused("anti_rollback_lock_not_held", partial(store.advance, record, accept_any))
    with store.locked():
        assert store.advance(record, accept_any)
    refused("anti_rollback_lock_not_held", partial(store.advance, record, accept_any))


def write_record(kit: Kit, key: str, sequence: int, value: Json) -> None:
    """What a writer of the anti-rollback directory can do without any signing key."""
    state = {"format": sr.TRUST_STATE_FORMAT, "key": key, "sequence": str(sequence), "value": value}
    path = record_path(kit, key)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_bytes(canonical(state))


def payload_digest(record: bytes) -> str:
    return hashlib.sha256(canonical(json.loads(record)["payload"])).hexdigest()


def test_n1_a_pending_rotation_outside_its_window_is_ignored(kit: Kit) -> None:
    verifier = kit.enrolled()
    genuine = record_path(kit, ANCHOR_KEY).read_bytes()
    enrolment = stored(kit, ANCHOR_KEY)["value"]["chain"][0]
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    old_grant = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    new_grant = sign(consent(), sr.GRANT_PURPOSE, successor, kit.operator)
    for changes, code in (
        ({"issued_at": stamp(-7200), "not_after": stamp(-1)}, "rotation_expired"),
        (
            {"issued_at": stamp(-60), "not_after": stamp(400 * 86400)},
            "rotation_validity_window_too_long",
        ),
        ({"issued_at": stamp(86400), "not_after": stamp(2 * 86400)}, "rotation_not_yet_valid"),
    ):
        rotation = kit.rotation(kit.customer, successor, **changes)
        refused(code, partial(verifier.rotate_customer_key, rotation))
        # A store writer installs the refused rotation ahead of the floor.
        write_record(kit, ANCHOR_KEY, 2, {"chain": [enrolment, json.loads(rotation)]})
        refused("customer_key_not_anchored", partial(verifier.verify, new_grant, sr.GRANT_PURPOSE))
        assert verifier.verify(old_grant, sr.GRANT_PURPOSE)
        record_path(kit, ANCHOR_KEY).write_bytes(genuine)


def test_n1_an_untouched_interrupted_rotation_counts_while_valid_then_reverts(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The residual risk named under Recovery: nothing took the lock before expiry."""
    verifier = kit.enrolled()
    old_floor = record_path(kit, REVISION_KEY).read_bytes()
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    verifier.rotate_customer_key(kit.rotation(kit.customer, successor))
    record_path(kit, REVISION_KEY).write_bytes(old_floor)  # the floor write was lost
    old_grant = sign(consent(not_after=stamp(80000)), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    new_grant = sign(consent(not_after=stamp(80000)), sr.GRANT_PURPOSE, successor, kit.operator)
    # Inside the rotation's window the pending rotation counts.
    assert verifier.verify(new_grant, sr.GRANT_PURPOSE)
    refused("customer_key_not_anchored", partial(verifier.verify, old_grant, sr.GRANT_PURPOSE))
    # Untouched until it expires, the anchor reverts to the floor instead of bricking.
    real_now = sr._utc_now
    monkeypatch.setattr(sr, "_utc_now", lambda: real_now() + timedelta(seconds=7200))
    refused("customer_key_not_anchored", partial(verifier.verify, new_grant, sr.GRANT_PURPOSE))
    assert verifier.verify(old_grant, sr.GRANT_PURPOSE)
    # A fresh rotation from the floor replaces the ignored pending tail.
    other = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    later = kit.rotation(kit.customer, other, issued_at=stamp(7000), not_after=stamp(20000))
    assert verifier.rotate_customer_key(later).anchor_revision == 2
    assert stored(kit, ANCHOR_KEY)["sequence"] == "3"
    other_grant = sign(consent(not_after=stamp(80000)), sr.GRANT_PURPOSE, other, kit.operator)
    assert verifier.verify(other_grant, sr.GRANT_PURPOSE)


def test_q4_a_locked_operation_in_window_rolls_the_floor_forward(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = kit.verifier()
    enrolment = kit.enrolment()
    verifier.pin_customer_anchor(enrolment)
    old_floor = record_path(kit, REVISION_KEY).read_bytes()
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    verifier.rotate_customer_key(kit.rotation(kit.customer, successor))
    record_path(kit, REVISION_KEY).write_bytes(old_floor)  # the floor write was lost
    assert stored(kit, REVISION_KEY)["sequence"] == "1"
    # Any locked operation on the engagement settles the pending rotation, even one that
    # is itself refused.
    refused("customer_anchor_already_pinned", partial(verifier.pin_customer_anchor, enrolment))
    assert stored(kit, REVISION_KEY)["sequence"] == "2"
    old_grant = sign(consent(not_after=stamp(80000)), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    new_grant = sign(consent(not_after=stamp(80000)), sr.GRANT_PURPOSE, successor, kit.operator)
    real_now = sr._utc_now
    monkeypatch.setattr(sr, "_utc_now", lambda: real_now() + timedelta(seconds=7200))
    # Past the rotation's window it no longer reverts: the old key stays rotated away.
    assert verifier.verify(new_grant, sr.GRANT_PURPOSE)
    refused("customer_key_not_anchored", partial(verifier.verify, old_grant, sr.GRANT_PURPOSE))


def test_q4_a_rotation_settles_a_pending_tail_before_extending_it(kit: Kit) -> None:
    verifier = kit.enrolled()
    old_floor = record_path(kit, REVISION_KEY).read_bytes()
    second = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    verifier.rotate_customer_key(kit.rotation(kit.customer, second))
    record_path(kit, REVISION_KEY).write_bytes(old_floor)
    third = Signer(sr.CUSTOMER_OWNER, registry_revision="3")
    assert verifier.rotate_customer_key(kit.rotation(second, third)).anchor_revision == 3
    assert stored(kit, REVISION_KEY)["sequence"] == "3"
    assert len(stored(kit, ANCHOR_KEY)["value"]["chain"]) == 3


def former_registry(kit: Kit, revision: str, keys: list[Json], former: list[Json]) -> bytes:
    return kit.registry(revision, keys=keys, former_custodian_keys=former)


def test_n2_custodian_rotation_keeps_old_anchors_through_the_verify_only_list(kit: Kit) -> None:
    kit.enrolled()
    custodian2 = Signer(sr.OPERATOR_CUSTODIAN, list(sr.REGISTRY_ROLES[sr.OPERATOR_CUSTODIAN]))
    current = [custodian2.entry(), kit.evaluator.entry(), kit.auditor.entry()]
    verifier = kit.verifier(former_registry(kit, "2", current, [kit.operator.public()]))
    grant = sign(consent(), sr.GRANT_PURPOSE, kit.customer, custodian2)
    assert verifier.verify(grant, sr.GRANT_PURPOSE)
    # The former key verifies existing enrolments only: never a grant, never a new pin.
    by_former = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    refused("key_unknown", partial(verifier.verify, by_former, sr.GRANT_PURPOSE))
    other = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    payload = kit.enrolment_payload(other, engagement_id="eng-sentinel-0002")
    new_pin = sign(payload, sr.ENROLMENT_PURPOSE, other, kit.operator)
    refused("key_unknown", partial(verifier.pin_customer_anchor, new_pin))
    current_pin = sign(payload, sr.ENROLMENT_PURPOSE, other, custodian2)
    assert verifier.pin_customer_anchor(current_pin).key_id == other.key_id
    # Dropping the former key deliberately invalidates the anchors it co-signed.
    dropped = kit.verifier(former_registry(kit, "3", current, []))
    refused("customer_anchor_unverifiable", partial(dropped.verify, grant, sr.GRANT_PURPOSE))


def test_n2_former_custodian_keys_never_reuse_another_role(kit: Kit) -> None:
    current = kit.issuers()
    for former, code in (
        ([kit.evaluator.public()], "trust_key_role_conflict"),
        ([kit.root.public()], "trust_key_role_conflict"),
        # A different public key under a current custodian key id.
        (
            [{**Signer(sr.OPERATOR_CUSTODIAN).public(), "key_id": kit.operator.key_id}],
            "trust_key_role_conflict",
        ),
        # A current custodian's public key under another id.
        ([{**kit.operator.public(), "key_id": "former-custodian-0003"}], "trust_key_role_conflict"),
        ([{**kit.auditor.public(), "key_id": "former-custodian-0001"}], "trust_key_role_conflict"),
    ):
        refused(code, partial(kit.verifier, former_registry(kit, "1", current, former)))
    retired = Signer(sr.OPERATOR_CUSTODIAN)
    twice = [retired.public(), {**retired.public(), "key_id": "former-custodian-0002"}]
    refused("trust_key_duplicate", partial(kit.verifier, former_registry(kit, "1", current, twice)))
    # A customer key may not be a former custodian key either.
    verifier = kit.verifier(former_registry(kit, "1", current, [retired.public()]))
    as_customer = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    as_customer.private, as_customer.der = retired.private, retired.der
    refused(
        "customer_key_not_distinct",
        partial(verifier.pin_customer_anchor, kit.enrolment(as_customer)),
    )


def test_n2_relisting_the_custodian_key_at_a_new_revision_keeps_anchors(kit: Kit) -> None:
    kit.enrolled()
    relisted = [kit.operator.entry(registry_revision="rev-2"), kit.evaluator.entry()]
    verifier = kit.verifier(kit.registry("2", keys=relisted))
    payload = consent()
    grant = canonical(
        {
            "payload": payload,
            "signatures": [
                kit.customer.signature(payload, sr.GRANT_PURPOSE),
                kit.operator.signature(payload, sr.GRANT_PURPOSE, key_registry_revision="rev-2"),
            ],
        }
    )
    assert verifier.verify(grant, sr.GRANT_PURPOSE)


def test_n2_removing_enrolment_purpose_keeps_anchors_but_refuses_new_pins(kit: Kit) -> None:
    kit.enrolled()
    narrowed = kit.operator.entry(purposes=[sr.GRANT_PURPOSE, sr.ROUTING_POLICY_PURPOSE])
    # The single permitted overlap: the same still-current key listed as verify-only.
    registry = former_registry(kit, "2", [narrowed, kit.evaluator.entry()], [kit.operator.public()])
    verifier = kit.verifier(registry)
    assert verifier.verify(
        sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator), sr.GRANT_PURPOSE
    )
    other = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    new_pin = sign(
        kit.enrolment_payload(other, engagement_id="eng-sentinel-0002"),
        sr.ENROLMENT_PURPOSE,
        other,
        kit.operator,
    )
    refused("key_purpose_not_permitted", partial(verifier.pin_customer_anchor, new_pin))


def test_q1_removing_enrolment_purpose_without_listing_makes_anchors_unverifiable(
    kit: Kit,
) -> None:
    kit.enrolled()
    narrowed = kit.operator.entry(purposes=[sr.GRANT_PURPOSE, sr.ROUTING_POLICY_PURPOSE])
    verifier = kit.verifier(kit.registry("2", keys=[narrowed, kit.evaluator.entry()]))
    grant = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    refused("customer_anchor_unverifiable", partial(verifier.verify, grant, sr.GRANT_PURPOSE))


def test_q1_a_custodian_key_without_enrolment_purpose_cannot_validate_a_stored_enrolment(
    kit: Kit,
) -> None:
    # Reviewer's repro: G is a current custodian key whose only purpose is grants.
    grantor = Signer(sr.OPERATOR_CUSTODIAN, [sr.GRANT_PURPOSE])
    verifier = kit.verifier(kit.registry(keys=[*kit.issuers(), grantor.entry()]))
    verifier.pin_customer_anchor(kit.enrolment())
    writer = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    forged = sign(kit.enrolment_payload(writer), sr.ENROLMENT_PURPOSE, writer, grantor)
    # The API refuses the forged enrolment ...
    other = sign(
        kit.enrolment_payload(writer, engagement_id="eng-sentinel-0002"),
        sr.ENROLMENT_PURPOSE,
        writer,
        grantor,
    )
    refused("key_purpose_not_permitted", partial(verifier.pin_customer_anchor, other))
    # ... and so does replay, after a store writer installs it with a matching floor.
    write_record(kit, ANCHOR_KEY, 2, {"chain": [json.loads(forged)]})
    write_record(kit, REVISION_KEY, 1, {"head_payload_sha256": payload_digest(forged)})
    minted = sign(consent(), sr.GRANT_PURPOSE, writer, grantor)
    refused("customer_anchor_unverifiable", partial(verifier.verify, minted, sr.GRANT_PURPOSE))
    genuine = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    refused("customer_anchor_unverifiable", partial(verifier.verify, genuine, sr.GRANT_PURPOSE))


def test_n2_a_key_id_names_one_public_key(kit: Kit) -> None:
    other_key = kit.evaluator.entry(
        key_id=kit.operator.key_id,
        role=sr.OPERATOR_CUSTODIAN,
        purposes=[sr.GRANT_PURPOSE],
        registry_revision="rev-2",
    )
    refused(
        "trust_key_duplicate",
        partial(kit.verifier, kit.registry(keys=[kit.operator.entry(), other_key])),
    )


OTHER = "eng-sentinel-0002"


def test_binding_another_engagements_chain_is_refused(kit: Kit) -> None:
    verifier = kit.verifier()
    other_customer = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    payload = kit.enrolment_payload(other_customer, engagement_id=OTHER)
    verifier.pin_customer_anchor(sign(payload, sr.ENROLMENT_PURPOSE, other_customer, kit.operator))
    # Copy engagement Y's chain and floor onto engagement X, rewriting the record keys.
    for source, target in (
        ("customer_anchor:" + OTHER, ANCHOR_KEY),
        ("customer_anchor_revision:" + OTHER, REVISION_KEY),
    ):
        copied = stored(kit, source)
        write_record(kit, target, int(copied["sequence"]), copied["value"])
    grant = sign(consent(), sr.GRANT_PURPOSE, other_customer, kit.operator)
    refused("customer_anchor_unverifiable", partial(verifier.verify, grant, sr.GRANT_PURPOSE))


def test_binding_a_rotation_for_another_engagement_is_refused(kit: Kit) -> None:
    verifier = kit.enrolled()
    # The same customer key is enrolled for engagement Y too.
    payload = kit.enrolment_payload(engagement_id=OTHER)
    verifier.pin_customer_anchor(sign(payload, sr.ENROLMENT_PURPOSE, kit.customer, kit.operator))
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    rotation_y = kit.rotation(kit.customer, successor, engagement_id=OTHER)
    enrolment_x = stored(kit, ANCHOR_KEY)["value"]["chain"][0]
    write_record(kit, ANCHOR_KEY, 2, {"chain": [enrolment_x, json.loads(rotation_y)]})
    grant = sign(consent(), sr.GRANT_PURPOSE, successor, kit.operator)
    # Pending (floor still at 1): the foreign rotation is ignored.
    refused("customer_key_not_anchored", partial(verifier.verify, grant, sr.GRANT_PURPOSE))
    # Accepted (floor advanced to cover it): the chain is unverifiable.
    write_record(kit, REVISION_KEY, 2, {"head_payload_sha256": payload_digest(rotation_y)})
    refused("customer_anchor_unverifiable", partial(verifier.verify, grant, sr.GRANT_PURPOSE))


# --- Cross-session review (refuse, never raise; stale re-pin) --------------------------


def _tlv(tag: int, body: bytes) -> bytes:
    size = len(body)
    if size < 128:
        length = bytes([size])
    elif size < 256:
        length = bytes([0x81, size])
    else:
        length = bytes([0x82, size >> 8, size & 0xFF])
    return bytes([tag]) + length + body


def _oid(dotted: str) -> bytes:
    parts = [int(part) for part in dotted.split(".")]
    out = bytes([40 * parts[0] + parts[1]])
    for value in parts[2:]:
        encoded = [value & 0x7F]
        value >>= 7
        while value:
            encoded.insert(0, (value & 0x7F) | 0x80)
            value >>= 7
        out += bytes(encoded)
    return _tlv(0x06, out)


UNSUPPORTED_CURVES = {
    "brainpoolP160r1": ("1.3.36.3.3.2.8.1.1.1", 20),
    "prime192v2": ("1.2.840.10045.3.1.2", 24),
    "sm2": ("1.2.156.10197.1.301", 32),
}


def unsupported_curve_key(curve: str) -> Json:
    """An EC SubjectPublicKeyInfo on a curve the cryptography backend cannot load."""
    oid, size = UNSUPPORTED_CURVES[curve]
    algorithm = _tlv(0x30, _oid("1.2.840.10045.2.1") + _oid(oid))
    der = _tlv(0x30, algorithm + _tlv(0x03, b"\x00\x04" + b"\x01" * (2 * size)))
    return {
        "key_id": f"customer-{curve.lower()}-0001",
        "public_key_der": base64.b64encode(der).decode("ascii"),
        "public_key_sha256": hashlib.sha256(der).hexdigest(),
    }


@pytest.mark.parametrize("curve", sorted(UNSUPPORTED_CURVES))
def test_review_unsupported_curves_refuse_on_pin_and_rotate(kit: Kit, curve: str) -> None:
    odd = unsupported_curve_key(curve)
    verifier = kit.verifier()
    payload = {**kit.enrolment_payload(), "customer_key": odd}
    enrolment = sign(payload, sr.ENROLMENT_PURPOSE, kit.customer, kit.operator)
    refused("trust_key_curve_invalid", partial(verifier.pin_customer_anchor, enrolment))
    verifier.pin_customer_anchor(kit.enrolment())
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    rotation_payload = {**kit.rotation_payload(kit.customer, successor), "customer_key": odd}
    rotation = sign(rotation_payload, sr.ROTATION_PURPOSE, kit.customer)
    refused("trust_key_curve_invalid", partial(verifier.rotate_customer_key, rotation))
    entry = {**kit.evaluator.entry(), **odd, "key_id": "evaluator-odd-curve-0001"}
    refused("trust_key_curve_invalid", partial(kit.verifier, kit.registry("2", keys=[entry])))


@pytest.mark.parametrize("curve", sorted(UNSUPPORTED_CURVES))
def test_review_unsupported_curve_in_the_chain(kit: Kit, curve: str) -> None:
    verifier = kit.enrolled()
    enrolment = stored(kit, ANCHOR_KEY)["value"]["chain"][0]
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    payload = {
        **kit.rotation_payload(kit.customer, successor),
        "customer_key": unsupported_curve_key(curve),
    }
    planted = sign(payload, sr.ROTATION_PURPOSE, kit.customer)
    genuine = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    # Pending tail: ignored like an out-of-window entry; the floor anchor still verifies.
    write_record(kit, ANCHOR_KEY, 2, {"chain": [enrolment, json.loads(planted)]})
    assert verifier.verify(genuine, sr.GRANT_PURPOSE)
    # Accepted chain (floor covers it): refused, never raised.
    write_record(kit, REVISION_KEY, 2, {"head_payload_sha256": payload_digest(planted)})
    refused("customer_anchor_unverifiable", partial(verifier.verify, genuine, sr.GRANT_PURPOSE))


def test_review_purpose_must_be_an_exact_string(kit: Kit) -> None:
    verifier = kit.verifier()
    record = sign(consent(), sr.VERDICT_PURPOSE, kit.evaluator)
    weird: list[Any] = [["x"], {"a": 1}, None, 3, b"opentour.pilot.independent-verdict.v1"]
    for purpose in weird:
        refused("purpose_unknown", partial(verifier.verify, record, purpose))


def test_review_unexpected_internal_errors_map_to_one_refusal(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = kit.enrolled()
    grant = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    rotation = kit.rotation(kit.customer, successor)
    enrolment = kit.enrolment()
    registry = kit.registry()

    def boom(*args: object, **kwargs: object) -> Any:
        raise RuntimeError("injected")

    with monkeypatch.context() as patch:
        patch.setattr(sr, "_open", boom)
        refused("verifier_internal_error", partial(verifier.verify, grant, sr.GRANT_PURPOSE))
        refused("verifier_internal_error", partial(verifier.pin_customer_anchor, enrolment))
        refused("verifier_internal_error", partial(verifier.rotate_customer_key, rotation))
    with monkeypatch.context() as patch:
        patch.setattr(sr, "_load_root_pin", boom)
        refused("verifier_internal_error", partial(sr.PilotVerifier, registry))
    with monkeypatch.context() as patch:
        patch.setattr(sr, "strict_json", boom)
        store = sr.TrustStateStore()
        refused("verifier_internal_error", partial(store.read, ANCHOR_KEY))
    # Nothing was accepted, and the genuine state still verifies afterwards.
    assert verifier.verify(grant, sr.GRANT_PURPOSE)
    # A caller-side misuse, such as an unknown keyword, is refused the same way.
    refused(
        "verifier_internal_error",
        partial(verifier.verify, grant, sr.GRANT_PURPOSE, now="later"),  # type: ignore[call-arg]
    )


def test_review_deleting_the_floor_after_an_expired_rotation_does_not_re_pin(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = kit.verifier()
    enrolment = kit.enrolment(not_after=stamp(20000))
    verifier.pin_customer_anchor(enrolment)
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    verifier.rotate_customer_key(kit.rotation(kit.customer, successor))
    record_path(kit, REVISION_KEY).unlink()
    real_now = sr._utc_now
    monkeypatch.setattr(sr, "_utc_now", lambda: real_now() + timedelta(seconds=7200))
    # The rotation is now expired and the enrolment still in its window.
    refused("customer_anchor_inconsistent", partial(verifier.pin_customer_anchor, enrolment))
    assert not record_path(kit, REVISION_KEY).exists()
    old_grant = sign(consent(not_after=stamp(80000)), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    refused("customer_anchor_inconsistent", partial(verifier.verify, old_grant, sr.GRANT_PURPOSE))


# --- Encoding and canonical form -----------------------------------------------------


def test_tampered_payload_byte_refused(kit: Kit) -> None:
    verifier = kit.enrolled()
    record = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    tampered = record.replace(b"org-sentinel-0001", b"org-sentinel-0002")
    assert tampered != record
    refused("payload_digest_mismatch", partial(verifier.verify, tampered, sr.GRANT_PURPOSE))
    refused(
        "signature_invalid",
        partial(verifier.verify, redigest(json.loads(tampered)), sr.GRANT_PURPOSE),
    )


def test_non_canonical_record_refused_even_with_valid_signatures(kit: Kit) -> None:
    verifier = kit.enrolled()
    value = envelope(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    for variant in (
        json.dumps(value).encode(),
        json.dumps(value, indent=1).encode(),
        canonical(value) + b"\n",
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(", ", ":")).encode(),
    ):
        refused("record_not_canonical", partial(verifier.verify, variant, sr.GRANT_PURPOSE))
    assert verifier.verify(canonical(value), sr.GRANT_PURPOSE) == value["payload"]


def test_non_json_profile_values_refused(kit: Kit) -> None:
    verifier = kit.enrolled()
    record = sign(consent(), sr.GRANT_PURPOSE, kit.customer, kit.operator)
    duplicate = record.replace(b'{"payload":', b'{"payload":{},"payload":', 1)
    refused("record_schema_invalid", partial(verifier.verify, duplicate, sr.GRANT_PURPOSE))
    text: Any = record.decode()
    refused("record_schema_invalid", partial(verifier.verify, text, sr.GRANT_PURPOSE))
    floating = record.replace(b'"engagement_epoch":3', b'"engagement_epoch":3.5')
    refused("record_schema_invalid", partial(verifier.verify, floating, sr.GRANT_PURPOSE))
    empty = canonical({"payload": consent(), "signatures": []})
    refused("record_schema_invalid", partial(verifier.verify, empty, sr.GRANT_PURPOSE))


@pytest.mark.parametrize(
    ("field_name", "value", "code"),
    [
        ("encoding", "der", "signature_encoding_invalid"),
        ("signature", "A" * 85, "signature_encoding_invalid"),
        ("signature", "A" * 86, "signature_encoding_invalid"),
        ("algorithm", "ES384", "profile_unsupported"),
        ("profile_version", "2.0", "profile_unsupported"),
    ],
)
def test_bad_encoding_or_profile_refused(kit: Kit, field_name: str, value: str, code: str) -> None:
    verifier = kit.verifier()
    record = envelope(consent(), sr.VERDICT_PURPOSE, kit.evaluator)
    record["signatures"][0][field_name] = value
    refused(code, partial(verifier.verify, canonical(record), sr.VERDICT_PURPOSE))


def test_fields_the_envelope_may_not_carry_are_refused(kit: Kit) -> None:
    verifier = kit.verifier()
    for where, name in (
        (None, "not_after"),
        (None, "signature"),
        (None, "trusted_keys"),
        (0, "public_key_der"),
        (0, "not_after"),
    ):
        record = envelope(consent(), sr.VERDICT_PURPOSE, kit.evaluator)
        target = record if where is None else record["signatures"][where]
        target[name] = stamp(10**7)
        refused(
            "record_field_forbidden",
            partial(verifier.verify, canonical(record), sr.VERDICT_PURPOSE),
        )


# --- Trust configuration -------------------------------------------------------------


def test_root_pin_required_and_not_yet_approved_in_repository(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()
    assert sr.APPROVED_ROOT_PIN_SHA256 is None
    assert sr.ROOT_PIN_PATH.parts[-2:] == ("trust", "pilot-registry-root.json")
    kit.monkeypatch.setattr(sr, "ANTI_ROLLBACK_DIRECTORY", kit.state)
    refused("root_pin_not_approved", partial(sr.PilotVerifier, b"{}"))


def test_root_pin_digest_mismatch_refused(kit: Kit) -> None:
    sr.ROOT_PIN_PATH.write_bytes(sr.ROOT_PIN_PATH.read_bytes() + b" ")
    refused("root_pin_digest_mismatch", kit.verifier)


def test_root_pin_missing_refused(kit: Kit) -> None:
    sr.ROOT_PIN_PATH.unlink()
    refused("root_pin_unavailable", kit.verifier)


def test_trust_path_and_time_are_not_caller_arguments() -> None:
    assert list(inspect.signature(sr.PilotVerifier).parameters) == ["registry_document"]
    assert list(inspect.signature(sr.TrustStateStore).parameters) == []
    assert list(inspect.signature(sr.PilotVerifier.verify).parameters) == [
        "self",
        "record",
        "purpose",
    ]
    for method in (sr.PilotVerifier.pin_customer_anchor, sr.PilotVerifier.rotate_customer_key):
        assert len(inspect.signature(method).parameters) == 2


def test_registry_signed_by_non_root_key_refused(kit: Kit) -> None:
    payload = kit.registry_payload()
    other_root = Signer(sr.OPERATOR_ROOT, [sr.REGISTRY_PURPOSE], registry_revision="root-1")
    refused("key_unknown", partial(kit.verifier, sign(payload, sr.REGISTRY_PURPOSE, other_root)))
    claims = kit.operator.signature(payload, sr.REGISTRY_PURPOSE, signer_role=sr.OPERATOR_ROOT)
    forged = canonical({"payload": payload, "signatures": [claims]})
    refused("key_unknown", partial(kit.verifier, forged))
    own_role = sign(payload, sr.REGISTRY_PURPOSE, kit.operator)
    refused("signature_role_unexpected", partial(kit.verifier, own_role))


def test_tampered_registry_refused(kit: Kit) -> None:
    stranger = Signer(sr.INDEPENDENT_EVALUATOR, [sr.VERDICT_PURPOSE])
    value = envelope(kit.registry_payload(), sr.REGISTRY_PURPOSE, kit.root)
    value["payload"]["keys"].append(stranger.entry())
    refused("payload_digest_mismatch", partial(kit.verifier, canonical(value)))
    refused("signature_invalid", partial(kit.verifier, redigest(value)))
    widened = envelope(kit.registry_payload(), sr.REGISTRY_PURPOSE, kit.root)
    widened["payload"]["keys"][2]["purposes"] = [sr.VERDICT_PURPOSE]
    refused("signature_invalid", partial(kit.verifier, redigest(widened)))


def test_registry_must_be_signed_under_the_registry_purpose(kit: Kit) -> None:
    record = sign(kit.registry_payload(), sr.GRANT_PURPOSE, kit.root)
    refused("purpose_mismatch", partial(kit.verifier, record))


def test_registry_binding_to_pinned_registry_id(kit: Kit) -> None:
    refused("registry_binding_invalid", partial(kit.verifier, kit.registry(registry_id="other")))


def test_trust_key_der_digest_mismatch_refused(kit: Kit) -> None:
    entry = kit.operator.entry(public_key_sha256=hashlib.sha256(b"other").hexdigest())
    refused("trust_key_digest_mismatch", partial(kit.verifier, kit.registry(keys=[entry])))
    kit.pin(root_keys=[kit.root.entry(public_key_sha256="0" * 64)])
    refused("trust_key_digest_mismatch", kit.verifier)


def test_trust_key_wrong_curve_refused(kit: Kit) -> None:
    p384 = Signer(sr.INDEPENDENT_EVALUATOR, [sr.VERDICT_PURPOSE], curve=ec.SECP384R1())
    refused("trust_key_curve_invalid", partial(kit.verifier, kit.registry(keys=[p384.entry()])))
    root384 = Signer(
        sr.OPERATOR_ROOT, [sr.REGISTRY_PURPOSE], registry_revision="root-1", curve=ec.SECP384R1()
    )
    kit.pin(root_keys=[root384.entry()])
    refused("trust_key_curve_invalid", kit.verifier)


def test_customer_key_wrong_curve_refused(kit: Kit) -> None:
    verifier = kit.verifier()
    p384 = Signer(sr.CUSTOMER_OWNER, registry_revision="1", curve=ec.SECP384R1())
    # The curve is checked before any signature, so the P-256 customer key signs here.
    record = sign(kit.enrolment_payload(p384), sr.ENROLMENT_PURPOSE, kit.customer, kit.operator)
    refused("trust_key_curve_invalid", partial(verifier.pin_customer_anchor, record))


def test_trust_key_duplicate_identity_refused(kit: Kit) -> None:
    clash = kit.evaluator.entry(key_id=kit.operator.key_id)
    refused(
        "trust_key_duplicate",
        partial(kit.verifier, kit.registry(keys=[kit.operator.entry(), clash])),
    )


@pytest.mark.parametrize(
    ("role", "purposes", "code"),
    [
        (sr.OPERATOR_CUSTODIAN, [], "trust_key_purposes_invalid"),
        (sr.OPERATOR_CUSTODIAN, ["opentour.pilot.other.v1"], "trust_key_purposes_invalid"),
        (sr.OPERATOR_CUSTODIAN, [sr.GRANT_PURPOSE] * 2, "trust_key_purposes_invalid"),
        (
            sr.OPERATOR_CUSTODIAN,
            [sr.GRANT_PURPOSE, sr.VERDICT_PURPOSE],
            "trust_key_purposes_invalid",
        ),
        (sr.BENCHMARK_AUDITOR, [sr.VERDICT_PURPOSE], "trust_key_purposes_invalid"),
        (sr.OPERATOR_CUSTODIAN, [sr.REGISTRY_PURPOSE], "trust_key_purposes_invalid"),
        (sr.OPERATOR_ROOT, [sr.REGISTRY_PURPOSE], "trust_key_role_invalid"),
        (sr.CUSTOMER_OWNER, [sr.GRANT_PURPOSE], "trust_key_role_invalid"),
        ("auditor", [sr.BENCHMARK_AUDIT_PURPOSE], "trust_key_role_invalid"),
    ],
)
def test_registry_entry_roles_and_purposes_validated(
    kit: Kit, role: str, purposes: list[str], code: str
) -> None:
    entry = kit.operator.entry(role=role, purposes=purposes)
    refused(code, partial(kit.verifier, kit.registry(keys=[entry])))


def test_root_key_may_only_sign_registries(kit: Kit) -> None:
    kit.pin(root_keys=[kit.root.entry(purposes=[sr.REGISTRY_PURPOSE, sr.GRANT_PURPOSE])])
    refused("trust_key_purposes_invalid", kit.verifier)
    kit.pin(root_keys=[kit.root.entry(role=sr.OPERATOR_CUSTODIAN)])
    refused("trust_key_role_invalid", kit.verifier)


@pytest.mark.parametrize(
    "key_id",
    [
        "arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-000000000000",
        "Operator-Key",
        "-operator",
        "o",
        "operator key",
        "operator/key",
    ],
)
def test_key_ids_are_strict_opaque_registry_identifiers(kit: Kit, key_id: str) -> None:
    entry = kit.operator.entry(key_id=key_id)
    refused("trust_key_id_invalid", partial(kit.verifier, kit.registry(keys=[entry])))


# --- Trusted time --------------------------------------------------------------------


def test_expired_record_refused(kit: Kit) -> None:
    verifier = kit.verifier()
    payload = consent(issued_at=stamp(-7200), not_after=stamp(-1))
    record = sign(payload, sr.VERDICT_PURPOSE, kit.evaluator)
    refused("record_expired", partial(verifier.verify, record, sr.VERDICT_PURPOSE))


def test_not_yet_valid_beyond_skew_refused_within_skew_accepted(kit: Kit) -> None:
    verifier = kit.verifier()
    skew = sr.ISSUED_AT_SKEW.total_seconds()
    early = sign(consent(issued_at=stamp(skew + 120)), sr.VERDICT_PURPOSE, kit.evaluator)
    refused("record_not_yet_valid", partial(verifier.verify, early, sr.VERDICT_PURPOSE))
    slight = sign(consent(issued_at=stamp(skew - 30)), sr.VERDICT_PURPOSE, kit.evaluator)
    assert verifier.verify(slight, sr.VERDICT_PURPOSE)


@pytest.mark.parametrize(
    "changes",
    [
        {"issued_at": None},
        {"not_after": "2030-01-01T00:00:00Z"},
        {"not_after": 1893456000},
        {"issued_at": stamp(60), "not_after": stamp(30)},
    ],
)
def test_validity_window_must_be_issuer_signed_and_well_formed(
    kit: Kit, changes: dict[str, object]
) -> None:
    verifier = kit.verifier()
    payload = {k: v for k, v in consent(**changes).items() if v is not None}
    record = sign(payload, sr.VERDICT_PURPOSE, kit.evaluator)
    refused("record_validity_window_invalid", partial(verifier.verify, record, sr.VERDICT_PURPOSE))


def test_caller_cannot_extend_validity(kit: Kit) -> None:
    verifier = kit.verifier()
    expired = consent(issued_at=stamp(-7200), not_after=stamp(-1))
    record = sign(expired, sr.VERDICT_PURPOSE, kit.evaluator)
    later = stamp(86400)
    # No time or validity argument exists on the verification path; passing one is
    # refused (never accepted, never raised as anything but a refusal).
    for keyword in ("now", "not_after", "clock", "at"):
        refused(
            "verifier_internal_error",
            partial(verifier.verify, record, sr.VERDICT_PURPOSE, **{keyword: later}),
        )
    # An unsigned later not_after is a forbidden field, wherever it is placed.
    value = json.loads(record)
    value["not_after"] = later
    refused(
        "record_field_forbidden", partial(verifier.verify, canonical(value), sr.VERDICT_PURPOSE)
    )
    # Rewriting the signed not_after breaks the signature, even with recomputed digests.
    value = json.loads(record)
    value["payload"]["not_after"] = later
    refused(
        "payload_digest_mismatch", partial(verifier.verify, canonical(value), sr.VERDICT_PURPOSE)
    )
    refused("signature_invalid", partial(verifier.verify, redigest(value), sr.VERDICT_PURPOSE))
    refused("record_expired", partial(verifier.verify, record, sr.VERDICT_PURPOSE))


def test_record_validity_is_capped_by_the_registry_window(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = kit.verifier(kit.registry(not_after=stamp(600)))
    record = sign(consent(not_after=stamp(86400)), sr.VERDICT_PURPOSE, kit.evaluator)
    assert verifier.verify(record, sr.VERDICT_PURPOSE)
    real_now = sr._utc_now
    monkeypatch.setattr(sr, "_utc_now", lambda: real_now() + timedelta(seconds=1200))
    refused("registry_expired", partial(verifier.verify, record, sr.VERDICT_PURPOSE))


def test_expired_registry_and_enrolment_refused(kit: Kit) -> None:
    stale = kit.registry(issued_at=stamp(-7200), not_after=stamp(-1))
    refused("registry_expired", partial(kit.verifier, stale))
    verifier = kit.verifier()
    old = kit.enrolment(issued_at=stamp(-7200), not_after=stamp(-1))
    refused("enrolment_expired", partial(verifier.pin_customer_anchor, old))
    assert not record_path(kit, ANCHOR_KEY).exists()


# --- Verify only ---------------------------------------------------------------------


def test_verifier_module_holds_no_signing_capability() -> None:
    source = inspect.getsource(sr)
    for forbidden in (
        "private_key",
        "generate_private_key",
        "load_pem_private_key",
        "load_der_private_key",
        "der_to_raw",
        "create_signature",
        ".sign(",
        "PRIVATE KEY",
    ):
        assert forbidden not in source
    assert not any(callable(getattr(sr, name, None)) for name in ("sign", "create_signature"))


# --- Purpose-scoped customer keys (OPE-566 decision 68da165c) ------------------------

GRANT, SPEND, ROUTING = sr.GRANT_PURPOSE, sr.SPEND_MANDATE_PURPOSE, sr.ROUTING_POLICY_PURPOSE
EVERY_CONSENT = frozenset({GRANT, SPEND, ROUTING, PUBLICATION})


def anchor_files(kit: Kit) -> tuple[bytes, bytes]:
    return record_path(kit, ANCHOR_KEY).read_bytes(), record_path(kit, REVISION_KEY).read_bytes()


def restore(kit: Kit, files: tuple[bytes, bytes]) -> None:
    record_path(kit, ANCHOR_KEY).write_bytes(files[0])
    record_path(kit, REVISION_KEY).write_bytes(files[1])


def test_scope_purpose_is_a_custodian_co_signed_chain_entry_not_a_consent_purpose(
    kit: Kit,
) -> None:
    assert sr.CONSENT_SCOPE_PURPOSE == SCOPE_PURPOSE
    assert sr.REQUIRED_ROLES[SCOPE_PURPOSE] == {sr.CUSTOMER_OWNER, sr.OPERATOR_CUSTODIAN}
    holders = {role for role, purposes in sr.REGISTRY_ROLES.items() if SCOPE_PURPOSE in purposes}
    assert holders == {sr.OPERATOR_CUSTODIAN}
    assert sr.REGISTRY_ROLES[sr.OPERATOR_CUSTODIAN] == EVERY_CONSENT | {
        sr.ENROLMENT_PURPOSE,
        SCOPE_PURPOSE,
    }
    # Unchanged: other registry roles, the root and the consent table itself.
    assert sr.REGISTRY_ROLES[sr.INDEPENDENT_EVALUATOR] == {sr.VERDICT_PURPOSE}
    assert sr.REGISTRY_ROLES[sr.BENCHMARK_AUDITOR] == {sr.BENCHMARK_AUDIT_PURPOSE}
    assert sr.ROOT_ROLES == {sr.OPERATOR_ROOT: {sr.REGISTRY_PURPOSE}}
    # (APPROVED_ROOT_PIN_SHA256 stays None: test_root_pin_required_and_not_yet_approved...)
    assert sr.CONSENT_PURPOSES == EVERY_CONSENT
    assert SCOPE_PURPOSE not in sr.RECORD_PURPOSES
    # Accepted only through its own entry point, never as a verified record.
    verifier = kit.enrolled()
    update = kit.scope_update([GRANT])
    refused("purpose_unknown", partial(verifier.verify, update, SCOPE_PURPOSE))
    assert len(inspect.signature(sr.PilotVerifier.update_customer_scope).parameters) == 2


def test_a_key_enrolled_without_publication_cannot_sign_one_until_a_co_signed_update(
    kit: Kit,
) -> None:
    registry = kit.registry()
    verifier = sr.PilotVerifier(registry)
    without = sorted(EVERY_CONSENT - {PUBLICATION})
    pinned = verifier.pin_customer_anchor(kit.enrolment(consent_purposes=without))
    assert pinned.consent_purposes == frozenset(without)
    publication = sign(consent(), PUBLICATION, kit.customer, kit.operator)
    refused("customer_purpose_not_enrolled", partial(verifier.verify, publication, PUBLICATION))
    assert verifier.verify(sign(consent(), GRANT, kit.customer, kit.operator), GRANT)
    updated = verifier.update_customer_scope(kit.scope_update(sorted(EVERY_CONSENT)))
    assert updated == sr.CustomerAnchor(
        ENGAGEMENT,
        kit.customer.key_id,
        2,
        hashlib.sha256(kit.customer.der).hexdigest(),
        EVERY_CONSENT,
    )
    customer = at(kit.customer, "2")
    allowed = sign(consent(), PUBLICATION, customer, kit.operator)
    assert verifier.verify(allowed, PUBLICATION)
    # Every customer signature names the anchor revision it was made under, so a record
    # signed under the narrower scope is never reinterpreted under the wider one.
    refused("customer_key_not_anchored", partial(verifier.verify, publication, PUBLICATION))
    # A fresh verifier re-derives the same scope from the stored chain.
    assert sr.PilotVerifier(registry).verify(allowed, PUBLICATION)


def test_a_narrowing_update_refuses_a_previously_allowed_purpose(kit: Kit) -> None:
    verifier = kit.enrolled()
    assert verifier.verify(sign(consent(), ROUTING, kit.customer, kit.operator), ROUTING)
    anchor = verifier.update_customer_scope(kit.scope_update([GRANT]))
    assert (anchor.anchor_revision, anchor.consent_purposes) == (2, {GRANT})
    customer = at(kit.customer, "2")
    assert verifier.verify(sign(consent(), GRANT, customer, kit.operator), GRANT)
    for purpose in sorted(EVERY_CONSENT - {GRANT}):
        record = sign(consent(), purpose, customer, kit.operator)
        refused("customer_purpose_not_enrolled", partial(verifier.verify, record, purpose))
    # Widening again is another co-signed update at the next revision.
    wider = verifier.update_customer_scope(kit.scope_update([GRANT, ROUTING], previous="2"))
    assert (wider.anchor_revision, wider.consent_purposes) == (3, {GRANT, ROUTING})
    record = sign(consent(), ROUTING, at(kit.customer, "3"), kit.operator)
    assert verifier.verify(record, ROUTING)


def test_rotation_inherits_the_scope_by_default(kit: Kit) -> None:
    verifier = kit.verifier()
    verifier.pin_customer_anchor(kit.enrolment(consent_purposes=[GRANT]))
    second = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    rotated = verifier.rotate_customer_key(kit.rotation(kit.customer, second))
    assert (rotated.key_id, rotated.consent_purposes) == (second.key_id, {GRANT})
    assert verifier.verify(sign(consent(), GRANT, second, kit.operator), GRANT)
    refused(
        "customer_purpose_not_enrolled",
        partial(verifier.verify, sign(consent(), PUBLICATION, second, kit.operator), PUBLICATION),
    )
    # After a scope update, the next rotation inherits the updated scope.
    widened = sorted({GRANT, PUBLICATION})
    verifier.update_customer_scope(kit.scope_update(widened, customer=second, previous="2"))
    third = Signer(sr.CUSTOMER_OWNER, registry_revision="4")
    final = verifier.rotate_customer_key(kit.rotation(at(second, "3"), third))
    assert (final.anchor_revision, final.consent_purposes) == (4, {GRANT, PUBLICATION})
    assert verifier.verify(sign(consent(), PUBLICATION, third, kit.operator), PUBLICATION)
    refused(
        "customer_purpose_not_enrolled",
        partial(verifier.verify, sign(consent(), SPEND, third, kit.operator), SPEND),
    )


def test_rotation_can_replace_the_scope(kit: Kit) -> None:
    verifier = kit.enrolled()
    second = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    for bad in ([], [GRANT, GRANT], [SPEND, GRANT], ["opentour.pilot.other.v1"], None, GRANT):
        refused(
            "customer_consent_scope_invalid",
            partial(
                verifier.rotate_customer_key,
                kit.rotation(kit.customer, second, consent_purposes=bad),
            ),
        )
    replaced = kit.rotation(kit.customer, second, consent_purposes=[PUBLICATION])
    anchor = verifier.rotate_customer_key(replaced)
    assert anchor.consent_purposes == {PUBLICATION}
    assert verifier.verify(sign(consent(), PUBLICATION, second, kit.operator), PUBLICATION)
    refused(
        "customer_purpose_not_enrolled",
        partial(verifier.verify, sign(consent(), GRANT, second, kit.operator), GRANT),
    )


def test_a_scope_update_needs_the_anchored_customer_and_a_permitted_custodian(
    kit: Kit,
) -> None:
    narrow = Signer(sr.OPERATOR_CUSTODIAN, [GRANT])
    other = Signer(sr.OPERATOR_CUSTODIAN, [SCOPE_PURPOSE])
    verifier = kit.verifier(kit.registry(keys=[*kit.issuers(), narrow.entry(), other.entry()]))
    verifier.pin_customer_anchor(kit.enrolment())
    payload = kit.scope_payload([GRANT])
    # The customer alone or the custodian alone.
    for signers in ((kit.customer,), (kit.operator,), (other,)):
        record = sign(payload, SCOPE_PURPOSE, *signers)
        refused("signature_role_missing", partial(verifier.update_customer_scope, record))
    # A custodian key whose registry entry lacks the scope purpose.
    record = sign(payload, SCOPE_PURPOSE, kit.customer, narrow)
    refused("key_purpose_not_permitted", partial(verifier.update_customer_scope, record))
    # Two operator keys, one claiming the customer role.
    claims = kit.operator.signature(payload, SCOPE_PURPOSE, signer_role=sr.CUSTOMER_OWNER)
    both_operator = canonical(
        {"payload": payload, "signatures": [claims, other.signature(payload, SCOPE_PURPOSE)]}
    )
    refused("customer_key_not_anchored", partial(verifier.update_customer_scope, both_operator))
    # Another registry role standing in for the custodian.
    stand_in = kit.evaluator.signature(payload, SCOPE_PURPOSE, signer_role=sr.OPERATOR_CUSTODIAN)
    by_evaluator = canonical(
        {
            "payload": payload,
            "signatures": [kit.customer.signature(payload, SCOPE_PURPOSE), stand_in],
        }
    )
    refused("key_role_not_permitted", partial(verifier.update_customer_scope, by_evaluator))
    # Rotation-style signers (customer plus a successor) are not a scope update.
    successor = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    pair = canonical(
        {
            "payload": payload,
            "signatures": [
                kit.customer.signature(payload, SCOPE_PURPOSE),
                successor.signature(
                    payload, SCOPE_PURPOSE, signer_role=sr.CUSTOMER_OWNER_SUCCESSOR
                ),
            ],
        }
    )
    refused("signature_role_unexpected", partial(verifier.update_customer_scope, pair))
    # Signatures made under another purpose do not carry over.
    as_grant = sign(payload, GRANT, kit.customer, kit.operator)
    refused("purpose_mismatch", partial(verifier.update_customer_scope, as_grant))
    # Nothing changed: the anchor is still at revision 1 with its full scope.
    assert stored(kit, REVISION_KEY)["sequence"] == "1"
    publication = sign(consent(), PUBLICATION, kit.customer, kit.operator)
    assert verifier.verify(publication, PUBLICATION)
    # Any current custodian whose purposes include the scope purpose may co-sign.
    assert verifier.update_customer_scope(kit.scope_update([GRANT], kit.customer, other))


def test_a_scope_update_by_a_stale_or_rotated_away_customer_key_is_refused(kit: Kit) -> None:
    verifier = kit.enrolled()
    second = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    verifier.rotate_customer_key(kit.rotation(kit.customer, second))
    # The rotated-away key, at its own revision or at the current one.
    for stale in (kit.customer, at(kit.customer, "2")):
        record = kit.scope_update([GRANT], stale, kit.operator, previous="2")
        refused("customer_key_not_anchored", partial(verifier.update_customer_scope, record))
    # The current key signing at a stale anchor revision.
    stale_revision = kit.scope_update([GRANT], at(second, "1"), kit.operator, customer=second)
    refused("customer_key_not_anchored", partial(verifier.update_customer_scope, stale_revision))
    # The current key at the current revision, naming the wrong key or skipping a revision.
    for changes in ({"customer_key_id": kit.customer.key_id}, {"anchor_revision": "4"}):
        payload = kit.scope_payload([GRANT], customer=second, previous="2", **changes)
        record = sign(payload, SCOPE_PURPOSE, second, kit.operator)
        refused("customer_scope_not_successor", partial(verifier.update_customer_scope, record))
    anchor = verifier.update_customer_scope(
        kit.scope_update([GRANT], customer=second, previous="2")
    )
    assert (anchor.key_id, anchor.anchor_revision) == (second.key_id, 3)
    # After an update, the same key signing at the pre-update revision is stale too.
    again = kit.scope_update([SPEND], second, kit.operator, customer=second, previous="2")
    refused("customer_key_not_anchored", partial(verifier.update_customer_scope, again))


INVALID_SCOPES: list[Any] = [
    [],
    ["opentour.pilot.other.v1"],
    [GRANT, GRANT],
    [SPEND, GRANT],
    sorted(EVERY_CONSENT, reverse=True),
    [GRANT, "opentour.pilot.other.v1"],
    [sr.ENROLMENT_PURPOSE],
    [SCOPE_PURPOSE],
    sorted({GRANT, SCOPE_PURPOSE}),
    sorted({GRANT, sr.ENROLMENT_PURPOSE}),
    [sr.ROTATION_PURPOSE],
    [sr.VERDICT_PURPOSE],
    [sr.REGISTRY_PURPOSE],
    [GRANT.upper()],
    [" " + GRANT],
    [GRANT + " "],
    [3],
    [[GRANT]],
    [None],
    None,
    GRANT,
    {GRANT: True},
]


@pytest.mark.parametrize("purposes", INVALID_SCOPES)
def test_an_invalid_scope_is_refused_on_update_and_enrolment(kit: Kit, purposes: Any) -> None:
    verifier = kit.enrolled()
    update = kit.scope_update(consent_purposes=purposes)  # an explicit null included
    refused("customer_consent_scope_invalid", partial(verifier.update_customer_scope, update))
    assert stored(kit, REVISION_KEY)["sequence"] == "1"
    other = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    payload = kit.enrolment_payload(other, engagement_id=OTHER, consent_purposes=purposes)
    enrolment = sign(payload, sr.ENROLMENT_PURPOSE, other, kit.operator)
    refused("customer_consent_scope_invalid", partial(verifier.pin_customer_anchor, enrolment))
    assert not record_path(kit, "customer_anchor:" + OTHER).exists()


def test_a_scope_must_hold_exact_strings() -> None:
    class Sneaky(str):
        pass

    assert sr._consent_scope([GRANT, PUBLICATION], fresh=True) == {GRANT, PUBLICATION}
    for value in ([Sneaky(GRANT)], [GRANT, Sneaky(PUBLICATION)], (GRANT,)):
        refused("customer_consent_scope_invalid", partial(sr._consent_scope, value, fresh=True))


def test_an_update_or_enrolment_without_a_scope_is_refused_as_missing(kit: Kit) -> None:
    verifier = kit.enrolled()
    payload = kit.scope_payload([GRANT])
    del payload["consent_purposes"]
    update = sign(payload, SCOPE_PURPOSE, kit.customer, kit.operator)
    refused("customer_consent_scope_missing", partial(verifier.update_customer_scope, update))
    other = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    enrolment = kit.enrolment_payload(other, engagement_id=OTHER)
    del enrolment["consent_purposes"]
    signed = sign(enrolment, sr.ENROLMENT_PURPOSE, other, kit.operator)
    refused("customer_consent_scope_missing", partial(verifier.pin_customer_anchor, signed))


def test_a_legacy_anchor_without_a_scope_is_never_defaulted(kit: Kit) -> None:
    verifier = kit.verifier()
    payload = kit.enrolment_payload()
    del payload["consent_purposes"]
    legacy = sign(payload, sr.ENROLMENT_PURPOSE, kit.customer, kit.operator)
    refused("customer_consent_scope_missing", partial(verifier.pin_customer_anchor, legacy))
    assert not record_path(kit, ANCHOR_KEY).exists()
    # A store already holding a validly signed pre-scope anchor, with a matching floor.
    write_record(kit, ANCHOR_KEY, 1, {"chain": [json.loads(legacy)]})
    write_record(kit, REVISION_KEY, 1, {"head_payload_sha256": payload_digest(legacy)})
    for purpose in sorted(EVERY_CONSENT):
        record = sign(consent(), purpose, kit.customer, kit.operator)
        refused("customer_consent_scope_missing", partial(verifier.verify, record, purpose))
    second = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    rotation = kit.rotation(kit.customer, second, consent_purposes=sorted(EVERY_CONSENT))
    refused("customer_consent_scope_missing", partial(verifier.rotate_customer_key, rotation))
    update = kit.scope_update()
    refused("customer_consent_scope_missing", partial(verifier.update_customer_scope, update))


def test_a_retired_purpose_drops_out_of_the_effective_scope(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = kit.registry()
    verifier = sr.PilotVerifier(registry)
    # Every accepted entry lists the purpose that is later retired: the enrolment, a
    # rotation that restates the scope and a scope update.
    verifier.pin_customer_anchor(kit.enrolment())
    second = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    every = sorted(EVERY_CONSENT)
    verifier.rotate_customer_key(kit.rotation(kit.customer, second, consent_purposes=every))
    verifier.update_customer_scope(kit.scope_update(every, customer=second, previous="2"))
    current = at(second, "3")
    publication = sign(consent(), PUBLICATION, current, kit.operator)
    assert verifier.verify(publication, PUBLICATION)
    monkeypatch.setattr(sr, "CONSENT_PURPOSES", EVERY_CONSENT - {PUBLICATION})
    refused("customer_purpose_not_enrolled", partial(verifier.verify, publication, PUBLICATION))
    # The anchored chain still re-verifies; the remaining purposes are unaffected.
    grant = sign(consent(), GRANT, current, kit.operator)
    assert sr.PilotVerifier(registry).verify(grant, GRANT)
    # A new scope may not list the retired purpose, and what resolves excludes it.
    refused(
        "customer_consent_scope_invalid",
        partial(
            verifier.update_customer_scope,
            kit.scope_update(every, customer=second, previous="3"),
        ),
    )
    third = Signer(sr.CUSTOMER_OWNER, registry_revision="4")
    rotated = verifier.rotate_customer_key(kit.rotation(current, third))
    assert rotated.consent_purposes == EVERY_CONSENT - {PUBLICATION}


def test_a_rolled_back_scope_update_is_refused_at_the_floor(kit: Kit) -> None:
    verifier = kit.enrolled()
    before = record_path(kit, ANCHOR_KEY).read_bytes()
    verifier.update_customer_scope(kit.scope_update([GRANT]))
    # Restoring the earlier chain (which allowed publication) is a rollback.
    record_path(kit, ANCHOR_KEY).write_bytes(before)
    for customer in (kit.customer, at(kit.customer, "2")):
        record = sign(consent(), PUBLICATION, customer, kit.operator)
        refused("customer_anchor_rolled_back", partial(verifier.verify, record, PUBLICATION))
    for update in (kit.scope_update([GRANT]), kit.scope_update([GRANT], previous="2")):
        refused("customer_anchor_rolled_back", partial(verifier.update_customer_scope, update))
    second = Signer(sr.CUSTOMER_OWNER, registry_revision="2")
    refused(
        "customer_anchor_rolled_back",
        partial(verifier.rotate_customer_key, kit.rotation(kit.customer, second)),
    )


def test_a_superseded_scope_update_cannot_be_replayed(kit: Kit) -> None:
    verifier = kit.enrolled()
    narrow = kit.scope_update([GRANT])
    verifier.update_customer_scope(narrow)
    verifier.update_customer_scope(kit.scope_update(sorted(EVERY_CONSENT), previous="2"))
    refused("customer_key_not_anchored", partial(verifier.update_customer_scope, narrow))
    record = sign(consent(), PUBLICATION, at(kit.customer, "3"), kit.operator)
    assert verifier.verify(record, PUBLICATION)


def test_re_presenting_a_scope_update_is_a_no_op_keyed_on_its_payload(kit: Kit) -> None:
    verifier = kit.enrolled()
    payload = kit.scope_payload([GRANT])
    first = sign(payload, SCOPE_PURPOSE, kit.customer, kit.operator)
    anchor = verifier.update_customer_scope(first)
    files = anchor_files(kit)
    assert verifier.update_customer_scope(first) == anchor
    # Re-signed (fresh ECDSA nonces) and in the other signature order: the same update.
    resigned = sign(payload, SCOPE_PURPOSE, kit.operator, kit.customer)
    assert resigned != first
    assert verifier.update_customer_scope(resigned) == anchor
    assert anchor_files(kit) == files
    # The same payload under a forged customer signature is refused, not a no-op.
    impostor = Signer(sr.CUSTOMER_OWNER, registry_revision="1")
    forged = canonical(
        {
            "payload": payload,
            "signatures": [
                impostor.signature(payload, SCOPE_PURPOSE, key_id=kit.customer.key_id),
                kit.operator.signature(payload, SCOPE_PURPOSE),
            ],
        }
    )
    refused("signature_invalid", partial(verifier.update_customer_scope, forged))
    # A custodian-only copy of the same payload is refused too.
    alone = sign(payload, SCOPE_PURPOSE, kit.operator)
    refused("signature_role_missing", partial(verifier.update_customer_scope, alone))
    # A different scope at the same revision is not a re-presentation.
    refused(
        "customer_key_not_anchored",
        partial(verifier.update_customer_scope, kit.scope_update([SPEND])),
    )
    assert anchor_files(kit) == files


def test_another_engagements_scope_update_cannot_apply(kit: Kit) -> None:
    verifier = kit.enrolled()
    refused(
        "customer_anchor_missing",
        partial(verifier.update_customer_scope, kit.scope_update([GRANT], engagement_id=OTHER)),
    )
    # The same customer key is enrolled for engagement Y too.
    other_enrolment = kit.enrolment_payload(engagement_id=OTHER)
    verifier.pin_customer_anchor(
        sign(other_enrolment, sr.ENROLMENT_PURPOSE, kit.customer, kit.operator)
    )
    update_y = kit.scope_update([GRANT], engagement_id=OTHER)
    anchor_y = verifier.update_customer_scope(update_y)
    assert (anchor_y.engagement_id, anchor_y.consent_purposes) == (OTHER, {GRANT})
    # Engagement X is untouched.
    publication = sign(consent(), PUBLICATION, kit.customer, kit.operator)
    assert verifier.verify(publication, PUBLICATION)
    # A store writer splicing Y's update into X's chain.
    enrolment_x = stored(kit, ANCHOR_KEY)["value"]["chain"][0]
    write_record(kit, ANCHOR_KEY, 2, {"chain": [enrolment_x, json.loads(update_y)]})
    grant = sign(consent(), GRANT, at(kit.customer, "2"), kit.operator)
    # Pending (floor still at 1): the foreign update is ignored.
    assert verifier.verify(publication, PUBLICATION)
    refused("customer_key_not_anchored", partial(verifier.verify, grant, GRANT))
    # Accepted (floor advanced to cover it): the chain is unverifiable.
    write_record(kit, REVISION_KEY, 2, {"head_payload_sha256": payload_digest(update_y)})
    refused("customer_anchor_unverifiable", partial(verifier.verify, grant, GRANT))


def test_an_interrupted_scope_update_is_pending_then_settled(kit: Kit) -> None:
    verifier = kit.enrolled()
    old_floor = record_path(kit, REVISION_KEY).read_bytes()
    update = kit.scope_update([GRANT])
    verifier.update_customer_scope(update)
    record_path(kit, REVISION_KEY).write_bytes(old_floor)  # the floor write was lost
    customer = at(kit.customer, "2")
    # Inside its window the pending update counts.
    assert verifier.verify(sign(consent(), GRANT, customer, kit.operator), GRANT)
    record = sign(consent(), PUBLICATION, customer, kit.operator)
    refused("customer_purpose_not_enrolled", partial(verifier.verify, record, PUBLICATION))
    old = sign(consent(), PUBLICATION, kit.customer, kit.operator)
    refused("customer_key_not_anchored", partial(verifier.verify, old, PUBLICATION))
    # Any locked operation settles it; re-presenting it is then a no-op.
    assert verifier.update_customer_scope(update).anchor_revision == 2
    assert stored(kit, REVISION_KEY)["sequence"] == "2"
    assert len(stored(kit, ANCHOR_KEY)["value"]["chain"]) == 2


def test_an_untouched_pending_scope_update_reverts_after_its_window(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = kit.enrolled()
    old_floor = record_path(kit, REVISION_KEY).read_bytes()
    verifier.update_customer_scope(kit.scope_update([GRANT]))
    record_path(kit, REVISION_KEY).write_bytes(old_floor)
    real_now = sr._utc_now
    monkeypatch.setattr(sr, "_utc_now", lambda: real_now() + timedelta(seconds=7200))
    # Expired and never settled: the anchor is derived at the floor, scope included.
    old = sign(consent(not_after=stamp(80000)), PUBLICATION, kit.customer, kit.operator)
    assert verifier.verify(old, PUBLICATION)
    new = sign(consent(not_after=stamp(80000)), GRANT, at(kit.customer, "2"), kit.operator)
    refused("customer_key_not_anchored", partial(verifier.verify, new, GRANT))
    # A fresh update from the floor replaces the ignored pending tail.
    later = kit.scope_update([SPEND], issued_at=stamp(7000), not_after=stamp(20000))
    assert verifier.update_customer_scope(later).consent_purposes == {SPEND}
    assert stored(kit, ANCHOR_KEY)["sequence"] == "3"


def test_an_unknown_chain_entry_kind_fails_closed(kit: Kit) -> None:
    verifier = kit.enrolled()
    genuine = anchor_files(kit)
    enrolment = stored(kit, ANCHOR_KEY)["value"]["chain"][0]
    grant = sign(consent(), GRANT, kit.customer, kit.operator)
    # Validly signed documents that are not chain entries, and malformed entries.
    mixed_payload = kit.scope_payload([GRANT])
    mixed = {
        "payload": mixed_payload,
        "signatures": [
            kit.customer.signature(mixed_payload, SCOPE_PURPOSE),
            kit.operator.signature(mixed_payload, sr.ROTATION_PURPOSE),
        ],
    }
    for planted in (
        json.loads(grant),
        json.loads(kit.enrolment()),
        mixed,
        {"payload": {}, "signatures": []},
        {"payload": {}, "signatures": [{"purpose": 7}]},
        {"payload": {}},
    ):
        write_record(kit, ANCHOR_KEY, 2, {"chain": [enrolment, planted]})
        # Pending: ignored, the anchor is derived at the floor.
        assert verifier.verify(grant, GRANT)
        # Covered by the floor: unverifiable, refused rather than raised.
        digest = hashlib.sha256(canonical(planted["payload"])).hexdigest()
        write_record(kit, REVISION_KEY, 2, {"head_payload_sha256": digest})
        refused("customer_anchor_unverifiable", partial(verifier.verify, grant, GRANT))
        restore(kit, genuine)
    assert verifier.verify(grant, GRANT)


def test_replaying_a_scope_update_needs_a_custodian_that_still_holds_the_scope_purpose(
    kit: Kit,
) -> None:
    kit.enrolled().update_customer_scope(kit.scope_update([GRANT]))
    payload = consent()
    customer = at(kit.customer, "2")
    # Relisted at a new registry revision with the purpose: the chain still verifies.
    relisted_keys = [kit.operator.entry(registry_revision="rev-2"), kit.evaluator.entry()]
    relisted = kit.verifier(kit.registry("2", keys=relisted_keys))
    grant = canonical(
        {
            "payload": payload,
            "signatures": [
                customer.signature(payload, GRANT),
                kit.operator.signature(payload, GRANT, key_registry_revision="rev-2"),
            ],
        }
    )
    assert relisted.verify(grant, GRANT)
    # The custodian keeps enrolment but loses the scope purpose: fail closed.
    narrowed = kit.operator.entry(purposes=[GRANT, sr.ENROLMENT_PURPOSE])
    dropped = kit.verifier(kit.registry("3", keys=[narrowed, kit.evaluator.entry()]))
    by_narrowed = sign(payload, GRANT, customer, kit.operator)
    refused("customer_anchor_unverifiable", partial(dropped.verify, by_narrowed, GRANT))
    # The verify-only former list re-verifies enrolments only, never scope updates.
    custodian2 = Signer(sr.OPERATOR_CUSTODIAN, sorted(sr.REGISTRY_ROLES[sr.OPERATOR_CUSTODIAN]))
    current = [custodian2.entry(), kit.evaluator.entry()]
    retired = kit.verifier(former_registry(kit, "4", current, [kit.operator.public()]))
    by_new = sign(payload, GRANT, customer, custodian2)
    refused("customer_anchor_unverifiable", partial(retired.verify, by_new, GRANT))


def test_scope_update_refuses_with_a_code_never_raises(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = kit.enrolled()
    update = kit.scope_update([GRANT])

    def boom(*args: object, **kwargs: object) -> Any:
        raise RuntimeError("injected")

    with monkeypatch.context() as patch:
        patch.setattr(sr, "_open", boom)
        refused("verifier_internal_error", partial(verifier.update_customer_scope, update))
    for junk in (b"", b"{}", b"[]", "text", None):
        with pytest.raises(sr.PilotRecordRefused):
            verifier.update_customer_scope(junk)  # type: ignore[arg-type]
    assert verifier.update_customer_scope(update).anchor_revision == 2
