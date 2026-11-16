"""Purpose-separated RFC8785/ES256 verification with independently supplied trust."""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from opentour_journal.models import (
    APPEND_PURPOSE,
    HEAD_PURPOSE,
    AppendIntent,
    AppendOutcome,
    AppendReceiptEnvelope,
    AppendReceiptPayload,
    AppendResponse,
    HeadState,
    JournalEntry,
    JournalError,
    JournalIdentity,
    JournalSignature,
    LiveHeadEnvelope,
    Purpose,
    WireModel,
)

P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
CHAIN_PURPOSE = "opentour.authority-journal.chain.v1"
GENESIS_PURPOSE = "opentour.authority-journal.genesis.v1"
FRONTIER_PURPOSE = "opentour.authority-journal.verified-frontier.v1"


def canonical(value: object) -> bytes:
    try:
        # The maintained canonicalizer validates its recursive JSON value at runtime.
        return rfc8785.dumps(cast(Any, value))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise JournalError("SCHEMA_INVALID", 422) from None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def model_digest(model: WireModel) -> str:
    return sha256(canonical(model.wire()))


def transcript(purpose: str, payload: object) -> bytes:
    return canonical({"purpose": purpose, "payload": payload})


def chain_digest(payload: AppendReceiptPayload) -> str:
    fields = (
        "journal",
        "organization_id",
        "previous_chain_sha256",
        "sequence",
        "operation_id",
        "phase",
        "content_sha256",
    )
    body = payload.wire()
    return sha256(transcript(CHAIN_PURPOSE, {k: body[k] for k in fields}))


def genesis_digest(journal: JournalIdentity, head: HeadState) -> str:
    return sha256(
        transcript(
            GENESIS_PURPOSE,
            {
                "journal": journal.wire(),
                "organization_id": head.organization_id,
                "initial_epoch": head.latest_epoch,
                "initial_authority_revision": head.latest_authority_revision,
            },
        )
    )


def der_to_raw(der: bytes) -> str:
    try:
        r, s = utils.decode_dss_signature(der)
        if utils.encode_dss_signature(r, s) != der:
            raise ValueError("noncanonical DER")
        if not (0 < r < P256_ORDER and 0 < s < P256_ORDER):
            raise ValueError("invalid scalar")
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    except (ValueError, TypeError, OverflowError):
        raise JournalError("JOURNAL_SIGNATURE_INVALID") from None


def raw_to_der(encoded: str) -> bytes:
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]{86}", encoded):
            raise ValueError("invalid encoding")
        raw = base64.b64decode(encoded + "==", altchars=b"-_", validate=True)
        if len(raw) != 64 or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != encoded:
            raise ValueError("noncanonical encoding")
        r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        if not (0 < r < P256_ORDER and 0 < s < P256_ORDER):
            raise ValueError("invalid scalar")
        return utils.encode_dss_signature(r, s)
    except (ValueError, TypeError, OverflowError):
        raise JournalError("JOURNAL_SIGNATURE_INVALID") from None


@dataclass(frozen=True)
class TrustedKey:
    """Loaded only from independently approved configuration, never an envelope."""

    key_id: str
    registry_revision: str
    public_key_der: bytes
    public_key_sha256: str
    purposes: frozenset[Purpose]
    live_head_eligible: bool

    def public_key(self) -> ec.EllipticCurvePublicKey:
        try:
            if sha256(self.public_key_der) != self.public_key_sha256:
                raise ValueError("key digest")
            key = serialization.load_der_public_key(self.public_key_der)
            if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
                key.curve, ec.SECP256R1
            ):
                raise ValueError("key curve")
            return key
        except (TypeError, ValueError):
            raise JournalError("JOURNAL_TRUST_INVALID", 503) from None


@dataclass(frozen=True)
class TrustPolicy:
    journal: JournalIdentity
    content_bucket: str
    keys: tuple[TrustedKey, ...]

    def validate(self) -> None:
        identities: set[tuple[str, str]] = set()
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.content_bucket):
            raise JournalError("JOURNAL_TRUST_INVALID", 503)
        if not self.keys:
            raise JournalError("JOURNAL_TRUST_INVALID", 503)
        prefix = f"arn:aws:kms:{self.journal.region}:{self.journal.audit_account_id}:key/"
        for key in self.keys:
            identity = (key.key_id, key.registry_revision)
            if (
                identity in identities
                or not key.key_id.startswith(prefix)
                or not re.fullmatch(r"[0-9a-f-]{36}", key.key_id[len(prefix) :])
                or not key.registry_revision
                or len(key.registry_revision) > 256
                or not key.purposes
                or not key.purposes <= {APPEND_PURPOSE, HEAD_PURPOSE}
                or (key.live_head_eligible and HEAD_PURPOSE not in key.purposes)
            ):
                raise JournalError("JOURNAL_TRUST_INVALID", 503)
            key.public_key()
            identities.add(identity)

    def key_for(self, signature: JournalSignature, purpose: Purpose) -> TrustedKey:
        for key in self.keys:
            if (
                key.key_id == signature.key_id
                and key.registry_revision == signature.key_registry_revision
                and purpose in key.purposes
                and (purpose != HEAD_PURPOSE or key.live_head_eligible)
            ):
                return key
        raise JournalError("JOURNAL_TRUST_INVALID", 503)


def create_signature(
    payload: WireModel,
    purpose: Purpose,
    key: TrustedKey,
    sign_digest: Callable[[bytes], bytes],
) -> JournalSignature:
    """Signer receives the SHA256 digest; KMS must use MessageType=DIGEST."""
    if purpose not in key.purposes:
        raise JournalError("JOURNAL_TRUST_INVALID", 503)
    payload_hash = model_digest(payload)
    digest = hashlib.sha256(transcript(purpose, payload.wire())).digest()
    encoded = der_to_raw(sign_digest(digest))
    signature = JournalSignature(
        profile_version="1.0",
        algorithm="ES256",
        encoding="base64url_unpadded_r_s_64",
        purpose=purpose,
        key_id=key.key_id,
        key_registry_revision=key.registry_revision,
        signer_role="audit_journal_issuer",
        payload_sha256=payload_hash,
        transcript_sha256=digest.hex(),
        signature=encoded,
    )
    _verify_signature(payload, signature, purpose, key)
    return signature


def _verify_signature(
    payload: WireModel,
    signature: JournalSignature,
    purpose: Purpose,
    key: TrustedKey,
) -> None:
    digest = hashlib.sha256(transcript(purpose, payload.wire())).digest()
    if (
        signature.purpose != purpose
        or signature.payload_sha256 != model_digest(payload)
        or signature.transcript_sha256 != digest.hex()
    ):
        raise JournalError("JOURNAL_SIGNATURE_INVALID")
    try:
        key.public_key().verify(
            raw_to_der(signature.signature), digest, ec.ECDSA(utils.Prehashed(hashes.SHA256()))
        )
    except InvalidSignature:
        raise JournalError("JOURNAL_SIGNATURE_INVALID") from None


class JournalVerifier:
    def __init__(self, trust: TrustPolicy) -> None:
        trust.validate()
        self.trust = trust

    def receipt(
        self,
        envelope: AppendReceiptEnvelope,
        organization_id: str,
        operation_id: str,
        phase: str,
    ) -> AppendReceiptPayload:
        payload = envelope.payload
        if (
            payload.journal != self.trust.journal
            or payload.organization_id != organization_id
            or payload.operation_id != operation_id
            or payload.phase != phase
            or payload.content_bucket_ref != self.trust.content_bucket
            or payload.content_key
            != content_key(organization_id, operation_id, phase, payload.content_sha256)
            or payload.chain_sha256 != chain_digest(payload)
        ):
            raise JournalError("JOURNAL_BINDING_INVALID")
        key = self.trust.key_for(envelope.signature, APPEND_PURPOSE)
        _verify_signature(payload, envelope.signature, APPEND_PURPOSE, key)
        return payload

    def response(self, response: AppendResponse, body: AppendIntent | AppendOutcome) -> None:
        phase = "intent" if isinstance(body, AppendIntent) else "outcome"
        payload = self.receipt(
            response.signed_receipt, body.organization_id, body.operation_id, phase
        )
        for field in AppendResponse.model_fields:
            if field != "signed_receipt" and getattr(response, field) != getattr(payload, field):
                raise JournalError("JOURNAL_BINDING_INVALID")
        if (
            payload.content_sha256 != model_digest(body)
            or payload.previous_chain_sha256 != body.expected_head_digest
            or payload.sequence != body.expected_head_sequence + 1
        ):
            raise JournalError("JOURNAL_BINDING_INVALID")

    def entry(self, entry: JournalEntry, organization_id: str, sequence: int) -> None:
        payload = self.receipt(
            entry.signed_receipt, organization_id, entry.operation_id, entry.phase
        )
        if payload.sequence != sequence or entry.signer_id != entry.signed_receipt.signature.key_id:
            raise JournalError("JOURNAL_BINDING_INVALID")
        for field in AppendReceiptPayload.model_fields:
            if getattr(entry, field) != getattr(payload, field):
                raise JournalError("JOURNAL_BINDING_INVALID")

    def live_head(self, envelope: LiveHeadEnvelope, organization_id: str, nonce: str) -> HeadState:
        payload = envelope.payload
        if (
            payload.journal != self.trust.journal
            or payload.head.organization_id != organization_id
            or payload.request_nonce != nonce
        ):
            raise JournalError("JOURNAL_BINDING_INVALID")
        key = self.trust.key_for(envelope.signature, HEAD_PURPOSE)
        _verify_signature(payload, envelope.signature, HEAD_PURPOSE, key)
        return payload.head


def content_key(organization_id: str, operation_id: str, phase: str, digest: str) -> str:
    # Hash path components: opaque IDs may contain separators and never choose S3 destinations.
    org = sha256(organization_id.encode("utf-8"))
    op = sha256(operation_id.encode("utf-8"))
    return f"journal/{org}/{op}/{phase}/{digest}.json"
