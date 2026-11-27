"""Verify issuer-signed pilot authority records. Verify only: this module cannot sign.

Governing rules: OPE-566 "Governing contract — owner ruling" as amended by "Amendment —
owner rulings 2026-11-27, second round".

Profile: the authority-journal profile, unchanged. ES256 over P-256, RFC 8785 canonical
JSON, signed bytes ``canonical({"purpose": p, "payload": payload})`` pre-hashed with
SHA-256, signature ``base64url_unpadded_r_s_64``. The canonicalisation, transcript, digest,
DER and strict JSON primitives are imported from ``opentour_journal`` so the two verifiers
cannot drift. A record is ``{"payload": {...}, "signatures": [...]}``: one signature per
required signer role, every signature over the same transcript.

Trust chain:

1. Kit configuration pins only the operator registry-root public key(s). The file path is
   fixed by the kit layout (``<kit>/trust/pilot-registry-root.json``) and its bytes are
   pinned by ``APPROVED_ROOT_PIN_SHA256`` in this module, which ships inside the verified
   kit. Neither is a caller argument. No root is approved yet, so loading refuses with
   ``root_pin_not_approved`` until the key ceremony has run and a kit pins its result.
2. The operator publishes an issuer registry, signed by a pinned root key under
   ``opentour.pilot.issuer-registry.v1``. Each entry is one hardware-token issuer key (or its
   sealed backup): an opaque registry key id, one issuer role, the purposes it may sign and
   its public-key DER pinned by SHA-256.
3. Customer-owner keys never come from the registry. Each engagement's customer key is
   anchored by an enrolment record signed by that customer key and co-signed by the
   operator custodian, pinned once at engagement start. Only a rotation record signed by the
   currently anchored (previous) customer key can replace it. No registry revision, however
   validly signed, can change an anchor or introduce a customer-owner key.

Required signer roles per purpose (``REQUIRED_ROLES``): customer consent records
(engagement grant, spend mandate, routing policy) need the customer owner and the operator
custodian; a verdict needs the independent evaluator; an audit receipt needs the benchmark
auditor; a registry needs the operator root. A missing, duplicated or unexpected role, one
key signing under two roles, or a key signing outside its own role or purposes is refused.

Anti-rollback state: the highest verified registry revision and every pinned customer
anchor are kept by ``TrustStateStore`` in a strict, versioned format
(``opentour.pilot.trust-state.v1``). Its location is fixed layout configuration
(``ANTI_ROLLBACK_DIRECTORY``), never a caller argument and never the worker data directory;
it must sit outside anything a worker snapshot or restore covers, like credentials. The
production location is reserved by the worker-host layout (OPE-577) and is not set here,
so the verifier refuses with ``anti_rollback_location_not_configured`` until it is. This
is local state: a party who can rewrite that location can reset it. Replay is then still
bounded by the kit-pinned ``minimum_registry_revision`` and by each document's signed
``not_after``.

Time: every signed document carries issuer-signed ``issued_at`` and ``not_after``. The local
clock only shortens validity: refuse after ``not_after`` or more than ``ISSUED_AT_SKEW``
before ``issued_at``. No argument supplies time, and the registry's own window is
re-checked on every verification.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Final, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from opentour_journal.crypto import canonical, raw_to_der, sha256, transcript
from opentour_journal.models import (
    Id,
    JournalError,
    Revision,
    Sha256,
    Timestamp,
    WireModel,
    strict_json,
)
from pydantic import Field, TypeAdapter, ValidationError

GRANT_PURPOSE: Final = "opentour.pilot.engagement-grant.v1"
SPEND_MANDATE_PURPOSE: Final = "opentour.pilot.spend-mandate.v1"
ROUTING_POLICY_PURPOSE: Final = "opentour.pilot.routing-policy.v1"
VERDICT_PURPOSE: Final = "opentour.pilot.independent-verdict.v1"
BENCHMARK_AUDIT_PURPOSE: Final = "opentour.pilot.benchmark-audit.v1"
REGISTRY_PURPOSE: Final = "opentour.pilot.issuer-registry.v1"
ENROLMENT_PURPOSE: Final = "opentour.pilot.customer-enrolment.v1"
ROTATION_PURPOSE: Final = "opentour.pilot.customer-key-rotation.v1"

CUSTOMER_OWNER: Final = "customer_owner"
OPERATOR_CUSTODIAN: Final = "operator_custodian"
INDEPENDENT_EVALUATOR: Final = "independent_evaluator"
BENCHMARK_AUDITOR: Final = "benchmark_auditor"
OPERATOR_ROOT: Final = "operator_root"

CONSENT_PURPOSES: Final = frozenset({GRANT_PURPOSE, SPEND_MANDATE_PURPOSE, ROUTING_POLICY_PURPOSE})
RECORD_PURPOSES: Final = CONSENT_PURPOSES | {VERDICT_PURPOSE, BENCHMARK_AUDIT_PURPOSE}
REQUIRED_ROLES: Final[Mapping[str, frozenset[str]]] = {
    GRANT_PURPOSE: frozenset({CUSTOMER_OWNER, OPERATOR_CUSTODIAN}),
    SPEND_MANDATE_PURPOSE: frozenset({CUSTOMER_OWNER, OPERATOR_CUSTODIAN}),
    ROUTING_POLICY_PURPOSE: frozenset({CUSTOMER_OWNER, OPERATOR_CUSTODIAN}),
    VERDICT_PURPOSE: frozenset({INDEPENDENT_EVALUATOR}),
    BENCHMARK_AUDIT_PURPOSE: frozenset({BENCHMARK_AUDITOR}),
    REGISTRY_PURPOSE: frozenset({OPERATOR_ROOT}),
    ENROLMENT_PURPOSE: frozenset({CUSTOMER_OWNER, OPERATOR_CUSTODIAN}),
    ROTATION_PURPOSE: frozenset({CUSTOMER_OWNER}),
}
# Purposes each registry role may be granted. Customer-owner keys are never registry keys,
# and the root role only exists in the kit-pinned root configuration.
REGISTRY_ROLES: Final[Mapping[str, frozenset[str]]] = {
    OPERATOR_CUSTODIAN: frozenset(
        {GRANT_PURPOSE, SPEND_MANDATE_PURPOSE, ROUTING_POLICY_PURPOSE, ENROLMENT_PURPOSE}
    ),
    INDEPENDENT_EVALUATOR: frozenset({VERDICT_PURPOSE}),
    BENCHMARK_AUDITOR: frozenset({BENCHMARK_AUDIT_PURPOSE}),
}
ROOT_ROLES: Final[Mapping[str, frozenset[str]]] = {OPERATOR_ROOT: frozenset({REGISTRY_PURPOSE})}

PROFILE_VERSION: Final = "1.0"
ALGORITHM: Final = "ES256"
ENCODING: Final = "base64url_unpadded_r_s_64"
ISSUED_AT_SKEW: Final = timedelta(seconds=60)
# Opaque registry identifiers (hardware-token keys), never provider resource names.
KEY_ID_PATTERN: Final = r"[a-z0-9](?:[a-z0-9._-]{1,126}[a-z0-9])"
MAX_SIGNATURES: Final = 4
MAX_CONFIG_BYTES: Final = 65536
MAX_STATE_BYTES: Final = 8192
TRUST_STATE_FORMAT: Final = "opentour.pilot.trust-state.v1"
FLOOR_NAME: Final = "pilot-issuer-registry-floor.json"
ANCHORS_NAME: Final = "pilot-customer-anchors"
LOCK_NAME: Final = "pilot-trust-state.lock"

# Fixed by the kit layout: <kit>/opentour-*.pyz/opentour_cell/pilot/ in a kit and
# services/cell-runtime/src/opentour_cell/pilot/ in the repository (kit root == CELL root).
ROOT_PIN_PATH = Path(os.path.abspath(__file__)).parents[3] / "trust" / "pilot-registry-root.json"
# SHA-256 of the approved root pin file. None until the operator root key ceremony has run.
APPROVED_ROOT_PIN_SHA256: str | None = None
# Absolute anti-rollback state location outside every worker snapshot, from the worker-host
# layout. None until OPE-577 reserves it; the data directory is never an acceptable value.
ANTI_ROLLBACK_DIRECTORY: Path | None = None

_ENVELOPE_FIELDS: Final = frozenset({"payload", "signatures"})
_SIGNATURE_FIELDS: Final = frozenset(
    {
        "profile_version",
        "algorithm",
        "encoding",
        "purpose",
        "signer_role",
        "key_id",
        "key_registry_revision",
        "payload_sha256",
        "transcript_sha256",
        "signature",
    }
)
_TIMESTAMP: Final = TypeAdapter(Timestamp)
_ID: Final = TypeAdapter(Id)
_Der = Annotated[str, Field(strict=True, min_length=1, max_length=4096)]


class PilotRecordRefused(Exception):
    """Safe refusal: carries a reason code only, never a wire value or key material."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _KeyEntry(WireModel):
    key_id: Id
    registry_revision: Id
    role: Id
    public_key_der: _Der
    public_key_sha256: Sha256
    purposes: list[Id]


class _RootPin(WireModel):
    profile_version: Literal["1.0"]
    kind: Literal["opentour.pilot.registry-root.v1"]
    registry_id: Id
    minimum_registry_revision: Revision
    root_keys: Annotated[list[_KeyEntry], Field(min_length=1)]


class _RegistryPayload(WireModel):
    profile_version: Literal["1.0"]
    registry_id: Id
    revision: Revision
    issued_at: Timestamp
    not_after: Timestamp
    keys: Annotated[list[_KeyEntry], Field(min_length=1)]


class _CustomerKey(WireModel):
    key_id: Id
    public_key_der: _Der
    public_key_sha256: Sha256


class _EnrolmentPayload(WireModel):
    profile_version: Literal["1.0"]
    engagement_id: Id
    anchor_revision: Revision
    customer_key: _CustomerKey
    issued_at: Timestamp
    not_after: Timestamp


class _RotationPayload(WireModel):
    profile_version: Literal["1.0"]
    engagement_id: Id
    previous_key_id: Id
    previous_anchor_revision: Revision
    anchor_revision: Revision
    customer_key: _CustomerKey
    issued_at: Timestamp
    not_after: Timestamp


class _FloorState(WireModel):
    format: Literal["opentour.pilot.trust-state.v1"]
    revision: Revision
    registry_sha256: Sha256


class _AnchorState(WireModel):
    format: Literal["opentour.pilot.trust-state.v1"]
    engagement_id: Id
    anchor_revision: Revision
    customer_key: _CustomerKey
    record_sha256: Sha256


class _Signature(WireModel):
    profile_version: Id
    algorithm: Id
    encoding: Id
    purpose: Id
    signer_role: Id
    key_id: Id
    key_registry_revision: Id
    payload_sha256: Sha256
    transcript_sha256: Sha256
    signature: Annotated[str, Field(strict=True, max_length=256)]


@dataclass(frozen=True)
class PilotTrustedKey:
    """Mirrors ``opentour_journal.crypto.TrustedKey`` with a pilot issuer role."""

    key_id: str
    registry_revision: str
    role: str
    public_key_sha256: str
    purposes: frozenset[str]
    public_key: ec.EllipticCurvePublicKey


@dataclass(frozen=True)
class CustomerAnchor:
    engagement_id: str
    key_id: str
    anchor_revision: int
    public_key_sha256: str


@dataclass(frozen=True)
class _Registry:
    revision: int
    issued_at: datetime
    not_after: datetime
    keys: Mapping[tuple[str, str], PilotTrustedKey]


Resolver = Callable[[_Signature], PilotTrustedKey]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _public_key(key_id: str, der_b64: str, pinned_sha256: str) -> ec.EllipticCurvePublicKey:
    # Mirrors TrustedKey.public_key with distinct reason codes; key ids are opaque ids.
    if not re.fullmatch(KEY_ID_PATTERN, key_id):
        raise PilotRecordRefused("trust_key_id_invalid")
    try:
        der = base64.b64decode(der_b64, validate=True)
        if base64.b64encode(der).decode("ascii") != der_b64:
            raise ValueError("noncanonical base64")
    except ValueError:
        raise PilotRecordRefused("trust_set_invalid") from None
    if sha256(der) != pinned_sha256:
        raise PilotRecordRefused("trust_key_digest_mismatch")
    try:
        key = serialization.load_der_public_key(der)
    except (TypeError, ValueError):
        raise PilotRecordRefused("trust_key_curve_invalid") from None
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise PilotRecordRefused("trust_key_curve_invalid")
    return key


def _trust_set(
    entries: list[_KeyEntry], roles: Mapping[str, frozenset[str]]
) -> dict[tuple[str, str], PilotTrustedKey]:
    # Mirrors TrustPolicy.validate: unique identities, non-empty bounded purposes, valid keys.
    keys: dict[tuple[str, str], PilotTrustedKey] = {}
    digests: set[str] = set()
    for entry in entries:
        identity = (entry.key_id, entry.registry_revision)
        if identity in keys:
            raise PilotRecordRefused("trust_key_duplicate")
        if entry.role not in roles:
            raise PilotRecordRefused("trust_key_role_invalid")
        purposes = frozenset(entry.purposes)
        if (
            not purposes
            or len(purposes) != len(entry.purposes)
            or not purposes <= roles[entry.role]
        ):
            raise PilotRecordRefused("trust_key_purposes_invalid")
        key = _public_key(entry.key_id, entry.public_key_der, entry.public_key_sha256)
        # One public key, one registry key id: a key may not appear under two identities.
        if entry.public_key_sha256 in digests and not any(
            k.public_key_sha256 == entry.public_key_sha256 and k.key_id == entry.key_id
            for k in keys.values()
        ):
            raise PilotRecordRefused("trust_key_duplicate")
        digests.add(entry.public_key_sha256)
        keys[identity] = PilotTrustedKey(
            entry.key_id,
            entry.registry_revision,
            entry.role,
            entry.public_key_sha256,
            purposes,
            key,
        )
    return keys


def _load_root_pin() -> tuple[_RootPin, dict[tuple[str, str], PilotTrustedKey]]:
    pinned = APPROVED_ROOT_PIN_SHA256
    if pinned is None:
        raise PilotRecordRefused("root_pin_not_approved")
    try:
        with ROOT_PIN_PATH.open("rb") as handle:
            data = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        raise PilotRecordRefused("root_pin_unavailable") from None
    if len(data) > MAX_CONFIG_BYTES:
        raise PilotRecordRefused("root_pin_invalid")
    if sha256(data) != pinned:
        raise PilotRecordRefused("root_pin_digest_mismatch")
    try:
        pin = _RootPin.model_validate(strict_json(data, limit=MAX_CONFIG_BYTES))
    except (JournalError, ValidationError):
        raise PilotRecordRefused("root_pin_invalid") from None
    return pin, _trust_set(pin.root_keys, ROOT_ROLES)


def _instant(value: object, subject: str) -> datetime:
    try:
        return datetime.fromisoformat(_TIMESTAMP.validate_python(value))
    except (ValidationError, ValueError):
        raise PilotRecordRefused(f"{subject}_validity_window_invalid") from None


def _check_window(issued_at: datetime, not_after: datetime, subject: str) -> None:
    # The local clock may only shorten the issuer-signed window, never extend it.
    if not_after <= issued_at:
        raise PilotRecordRefused(f"{subject}_validity_window_invalid")
    now = _utc_now()
    if now > not_after:
        raise PilotRecordRefused(f"{subject}_expired")
    if now < issued_at - ISSUED_AT_SKEW:
        raise PilotRecordRefused(f"{subject}_not_yet_valid")


def _open(record: object, subject: str) -> tuple[dict[str, object], list[_Signature]]:
    """Strict structure only. Nothing returned here is authenticated yet."""
    if type(record) is not bytes:
        raise PilotRecordRefused(f"{subject}_schema_invalid")
    try:
        value = strict_json(record)
        canonical_record = canonical(value)
    except JournalError:
        raise PilotRecordRefused(f"{subject}_schema_invalid") from None
    if not isinstance(value, dict):
        raise PilotRecordRefused(f"{subject}_schema_invalid")
    # One accepted byte representation: no whitespace, key-order or escape malleability.
    if canonical_record != record:
        raise PilotRecordRefused(f"{subject}_not_canonical")
    if not set(value) <= _ENVELOPE_FIELDS:
        raise PilotRecordRefused(f"{subject}_field_forbidden")
    payload, raw = value.get("payload"), value.get("signatures")
    if (
        not isinstance(payload, dict)
        or not isinstance(raw, list)
        or not 1 <= len(raw) <= MAX_SIGNATURES
        or not all(isinstance(item, dict) for item in raw)
    ):
        raise PilotRecordRefused(f"{subject}_schema_invalid")
    signatures = []
    for item in raw:
        if not set(item) <= _SIGNATURE_FIELDS:
            raise PilotRecordRefused(f"{subject}_field_forbidden")
        try:
            signatures.append(_Signature.model_validate(item))
        except ValidationError:
            raise PilotRecordRefused(f"{subject}_schema_invalid") from None
    return payload, signatures


def _authenticate(
    payload: dict[str, object],
    signatures: list[_Signature],
    purpose: str,
    resolve: Mapping[str, Resolver],
    subject: str,
) -> None:
    for signature in signatures:
        if signature.profile_version != PROFILE_VERSION or signature.algorithm != ALGORITHM:
            raise PilotRecordRefused("profile_unsupported")
        if signature.encoding != ENCODING:
            raise PilotRecordRefused("signature_encoding_invalid")
        if signature.purpose != purpose:
            raise PilotRecordRefused("purpose_mismatch")
    roles = [signature.signer_role for signature in signatures]
    required = REQUIRED_ROLES[purpose]
    if len(set(roles)) != len(roles):
        raise PilotRecordRefused("signature_role_duplicate")
    if not set(roles) <= required:
        raise PilotRecordRefused("signature_role_unexpected")
    if set(roles) != required:
        raise PilotRecordRefused("signature_role_missing")
    # One key may not stand in for two roles, by key id (claimed) or by public key (resolved).
    if len({signature.key_id for signature in signatures}) != len(signatures):
        raise PilotRecordRefused("signature_key_reused")
    keys = [resolve[signature.signer_role](signature) for signature in signatures]
    if len({key.public_key_sha256 for key in keys}) != len(keys):
        raise PilotRecordRefused("signature_key_reused")
    digest = hashlib.sha256(transcript(purpose, payload)).digest()
    payload_sha256 = sha256(canonical(payload))
    for signature, key in zip(signatures, keys, strict=True):
        if key.role != signature.signer_role:
            raise PilotRecordRefused("key_role_not_permitted")
        if purpose not in key.purposes:
            raise PilotRecordRefused("key_purpose_not_permitted")
        try:
            der = raw_to_der(signature.signature)
        except JournalError:
            raise PilotRecordRefused("signature_encoding_invalid") from None
        if signature.payload_sha256 != payload_sha256:
            raise PilotRecordRefused("payload_digest_mismatch")
        if signature.transcript_sha256 != digest.hex():
            raise PilotRecordRefused("transcript_digest_mismatch")
        try:
            key.public_key.verify(der, digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        except InvalidSignature:
            raise PilotRecordRefused("signature_invalid") from None
    _check_window(
        _instant(payload.get("issued_at"), subject),
        _instant(payload.get("not_after"), subject),
        subject,
    )


def _registry_resolver(keys: Mapping[tuple[str, str], PilotTrustedKey]) -> Resolver:
    def resolve(signature: _Signature) -> PilotTrustedKey:
        key = keys.get((signature.key_id, signature.key_registry_revision))
        if key is None:
            known = any(key_id == signature.key_id for key_id, _ in keys)
            raise PilotRecordRefused("key_revision_not_active" if known else "key_unknown")
        return key

    return resolve


def _customer_key(key: _CustomerKey, revision: int, purposes: frozenset[str]) -> PilotTrustedKey:
    public_key = _public_key(key.key_id, key.public_key_der, key.public_key_sha256)
    return PilotTrustedKey(
        key.key_id, str(revision), CUSTOMER_OWNER, key.public_key_sha256, purposes, public_key
    )


def _fixed_resolver(key: PilotTrustedKey, code: str = "customer_key_not_anchored") -> Resolver:
    def resolve(signature: _Signature) -> PilotTrustedKey:
        if (signature.key_id, signature.key_registry_revision) != (
            key.key_id,
            key.registry_revision,
        ):
            raise PilotRecordRefused(code)
        return key

    return resolve


def _write_atomic(directory: Path, name: str, body: bytes) -> None:
    temporary = directory / (name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, directory / name)
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_state(path: Path, code: str) -> object | None:
    if path.is_symlink():
        raise PilotRecordRefused(code)
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise PilotRecordRefused(code) from None
    try:
        return strict_json(data, limit=MAX_STATE_BYTES)
    except JournalError:
        raise PilotRecordRefused(code) from None


class TrustStateStore:
    """Anti-rollback state: monotonic registry revision floor and per-engagement anchors.

    Located only by ``ANTI_ROLLBACK_DIRECTORY``. Every file is canonical JSON in the
    ``opentour.pilot.trust-state.v1`` format; anything else fails closed. Writes are atomic
    and serialized by an exclusive lock. The store enforces state transitions only; the
    verifier authenticates the signed documents that justify them.
    """

    def __init__(self) -> None:
        location = ANTI_ROLLBACK_DIRECTORY
        if location is None:
            raise PilotRecordRefused("anti_rollback_location_not_configured")
        directory = Path(location)
        if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
            raise PilotRecordRefused("anti_rollback_state_unavailable")
        self._directory = directory

    @contextmanager
    def locked(self) -> Iterator[None]:
        try:
            lock = os.open(
                self._directory / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
            )
        except OSError:
            raise PilotRecordRefused("anti_rollback_state_unavailable") from None
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
        except OSError:
            raise PilotRecordRefused("anti_rollback_state_unavailable") from None
        finally:
            os.close(lock)

    def advance_registry_floor(self, revision: int, document_sha256: str) -> None:
        """Hold under ``locked``. Lower revisions and same-revision equivocation refuse."""
        value = _read_state(self._directory / FLOOR_NAME, "registry_floor_unreadable")
        if value is not None:
            try:
                floor = _FloorState.model_validate(value)
            except ValidationError:
                raise PilotRecordRefused("registry_floor_unreadable") from None
            if revision < int(floor.revision):
                raise PilotRecordRefused("registry_revision_rolled_back")
            if revision == int(floor.revision):
                if document_sha256 != floor.registry_sha256:
                    raise PilotRecordRefused("registry_revision_equivocated")
                return
        state = {
            "format": TRUST_STATE_FORMAT,
            "revision": str(revision),
            "registry_sha256": document_sha256,
        }
        _write_atomic(self._directory, FLOOR_NAME, canonical(state))

    def anchor(self, engagement_id: str) -> _AnchorState | None:
        path = self._directory / ANCHORS_NAME / self._anchor_name(engagement_id)
        value = _read_state(path, "customer_anchor_unreadable")
        if value is None:
            return None
        try:
            state = _AnchorState.model_validate(value)
        except ValidationError:
            raise PilotRecordRefused("customer_anchor_unreadable") from None
        if state.engagement_id != engagement_id:
            raise PilotRecordRefused("customer_anchor_unreadable")
        return state

    def pin_anchor(self, engagement_id: str, key: _CustomerKey, record_sha256: str) -> None:
        """Hold under ``locked``. First anchor only; an identical enrolment is idempotent."""
        current = self.anchor(engagement_id)
        if current is not None:
            if current.record_sha256 != record_sha256:
                raise PilotRecordRefused("customer_anchor_already_pinned")
            return
        self._write_anchor(engagement_id, 1, key, record_sha256)

    def replace_anchor(
        self, engagement_id: str, revision: int, key: _CustomerKey, record_sha256: str
    ) -> None:
        """Hold under ``locked``. Only the immediate successor revision may replace."""
        current = self.anchor(engagement_id)
        if current is None:
            raise PilotRecordRefused("customer_anchor_missing")
        if revision != int(current.anchor_revision) + 1:
            raise PilotRecordRefused("customer_rotation_not_successor")
        self._write_anchor(engagement_id, revision, key, record_sha256)

    @staticmethod
    def _anchor_name(engagement_id: str) -> str:
        return sha256(engagement_id.encode("utf-8")) + ".json"

    def _write_anchor(
        self, engagement_id: str, revision: int, key: _CustomerKey, record_sha256: str
    ) -> None:
        directory = self._directory / ANCHORS_NAME
        if directory.is_symlink():
            raise PilotRecordRefused("anti_rollback_state_unavailable")
        directory.mkdir(mode=0o700, exist_ok=True)
        state = {
            "format": TRUST_STATE_FORMAT,
            "engagement_id": engagement_id,
            "anchor_revision": str(revision),
            "customer_key": key.wire(),
            "record_sha256": record_sha256,
        }
        _write_atomic(directory, self._anchor_name(engagement_id), canonical(state))


class PilotVerifier:
    """Verify-only. Trust comes from the kit-pinned root and pinned customer anchors.

    ``registry_document`` is the operator-signed issuer registry; it is authenticated
    against the pinned root, so where it was read from confers no authority. Neither the
    root nor the anti-rollback location is a caller argument.
    """

    def __init__(self, registry_document: bytes) -> None:
        self._store = TrustStateStore()
        pin, roots = _load_root_pin()
        payload, signatures = _open(registry_document, "registry")
        _authenticate(
            payload,
            signatures,
            REGISTRY_PURPOSE,
            {OPERATOR_ROOT: _registry_resolver(roots)},
            "registry",
        )
        try:
            registry = _RegistryPayload.model_validate(payload)
        except ValidationError:
            raise PilotRecordRefused("registry_schema_invalid") from None
        if registry.registry_id != pin.registry_id:
            raise PilotRecordRefused("registry_binding_invalid")
        revision = int(registry.revision)
        if revision < int(pin.minimum_registry_revision):
            raise PilotRecordRefused("registry_revision_below_kit_minimum")
        keys = _trust_set(registry.keys, REGISTRY_ROLES)
        with self._store.locked():
            self._store.advance_registry_floor(revision, sha256(registry_document))
        self._registry = _Registry(
            revision,
            datetime.fromisoformat(registry.issued_at),
            datetime.fromisoformat(registry.not_after),
            keys,
        )

    @property
    def registry_revision(self) -> int:
        return self._registry.revision

    def verify(self, record: bytes, purpose: str) -> dict[str, object]:
        """Return the verified payload of ``record`` for ``purpose`` or refuse with a code."""
        if purpose not in RECORD_PURPOSES:
            raise PilotRecordRefused("purpose_unknown")
        self._registry_current()
        payload, signatures = _open(record, "record")
        resolvers = self._issuer_resolvers()
        if purpose in CONSENT_PURPOSES:
            engagement = payload.get("engagement_id")
            try:
                engagement_id = _ID.validate_python(engagement)
            except ValidationError:
                raise PilotRecordRefused("record_schema_invalid") from None
            anchor = self._anchor(engagement_id)
            if anchor is None:
                raise PilotRecordRefused("customer_anchor_missing")
            resolvers[CUSTOMER_OWNER] = _fixed_resolver(anchor)
        _authenticate(payload, signatures, purpose, resolvers, "record")
        return payload

    def pin_customer_anchor(self, enrolment: bytes) -> CustomerAnchor:
        """Pin an engagement's customer key at engagement start. Never replaces an anchor."""
        self._registry_current()
        payload, signatures = _open(enrolment, "enrolment")
        try:
            body = _EnrolmentPayload.model_validate(payload)
        except ValidationError:
            raise PilotRecordRefused("enrolment_schema_invalid") from None
        if int(body.anchor_revision) != 1:
            raise PilotRecordRefused("customer_anchor_revision_invalid")
        customer = _customer_key(body.customer_key, 1, CONSENT_PURPOSES | {ENROLMENT_PURPOSE})
        self._require_distinct(customer)
        resolvers = self._issuer_resolvers()
        # Proof of possession: the enrolled key itself must sign its enrolment.
        resolvers[CUSTOMER_OWNER] = _fixed_resolver(customer, "enrolment_key_mismatch")
        _authenticate(payload, signatures, ENROLMENT_PURPOSE, resolvers, "enrolment")
        record_sha256 = sha256(enrolment)
        with self._store.locked():
            self._store.pin_anchor(body.engagement_id, body.customer_key, record_sha256)
        return CustomerAnchor(body.engagement_id, customer.key_id, 1, customer.public_key_sha256)

    def rotate_customer_key(self, rotation: bytes) -> CustomerAnchor:
        """Replace an anchor only on a rotation signed by the currently anchored key."""
        self._registry_current()
        payload, signatures = _open(rotation, "rotation")
        try:
            body = _RotationPayload.model_validate(payload)
        except ValidationError:
            raise PilotRecordRefused("rotation_schema_invalid") from None
        revision = int(body.anchor_revision)
        with self._store.locked():
            previous = self._anchor(body.engagement_id, ROTATION_PURPOSE)
            if previous is None:
                raise PilotRecordRefused("customer_anchor_missing")
            _authenticate(
                payload,
                signatures,
                ROTATION_PURPOSE,
                {CUSTOMER_OWNER: _fixed_resolver(previous)},
                "rotation",
            )
            if (
                body.previous_key_id != previous.key_id
                or body.previous_anchor_revision != previous.registry_revision
                or revision != int(previous.registry_revision) + 1
            ):
                raise PilotRecordRefused("customer_rotation_not_successor")
            successor = _customer_key(body.customer_key, revision, CONSENT_PURPOSES)
            self._require_distinct(successor)
            if successor.public_key_sha256 == previous.public_key_sha256:
                raise PilotRecordRefused("customer_key_not_distinct")
            self._store.replace_anchor(
                body.engagement_id, revision, body.customer_key, sha256(rotation)
            )
        return CustomerAnchor(
            body.engagement_id, successor.key_id, revision, successor.public_key_sha256
        )

    def _issuer_resolvers(self) -> dict[str, Resolver]:
        resolve = _registry_resolver(self._registry.keys)
        return {role: resolve for role in REGISTRY_ROLES}

    def _registry_current(self) -> None:
        _check_window(self._registry.issued_at, self._registry.not_after, "registry")

    def _require_distinct(self, customer: PilotTrustedKey) -> None:
        # The operator must not be able to enrol one of its own registry keys as a customer.
        if any(
            key.key_id == customer.key_id or key.public_key_sha256 == customer.public_key_sha256
            for key in self._registry.keys.values()
        ):
            raise PilotRecordRefused("customer_key_not_distinct")

    def _anchor(self, engagement_id: str, purpose: str | None = None) -> PilotTrustedKey | None:
        state = self._store.anchor(engagement_id)
        if state is None:
            return None
        purposes = CONSENT_PURPOSES if purpose is None else frozenset({purpose})
        return _customer_key(state.customer_key, int(state.anchor_revision), purposes)
