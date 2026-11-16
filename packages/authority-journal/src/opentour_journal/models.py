"""Strict journal wire models. Validation establishes shape, never authority."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated, ClassVar, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

MAX_COUNTER = 9007199254740991
MAX_REVISION = 9223372036854775807
MAX_WIRE_BYTES = 65536
MAX_REPLAY_ENTRIES = 10000
# Evidence has a separate finite budget; the entry-count ceiling does not waive it.
MAX_MANIFEST_BYTES = 32 * 1024 * 1024


class JournalError(Exception):
    """Safe boundary error; never attach a provider response or wire value."""

    def __init__(self, code: str, status: int = 409) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _revision(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > MAX_REVISION:
        raise ValueError("invalid revision")
    return value


def _timestamp(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value):
        raise ValueError("invalid timestamp")
    datetime.fromisoformat(value)
    return value


Id = Annotated[str, Field(strict=True, min_length=1, max_length=256)]
Sha256 = Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]
Counter = Annotated[int, Field(strict=True, ge=0, le=MAX_COUNTER)]
PositiveCounter = Annotated[int, Field(strict=True, ge=1, le=MAX_COUNTER)]
Revision = Annotated[str, Field(strict=True), AfterValidator(_revision)]
Timestamp = Annotated[str, Field(strict=True), AfterValidator(_timestamp)]
Phase = Literal["intent", "outcome"]
MutationKind = Literal["grant", "restrict", "tombstone", "enrollment", "recovery"]
APPEND_PURPOSE: Final = "opentour.authority-journal.append-receipt.v1"
HEAD_PURPOSE: Final = "opentour.authority-journal.live-head.v1"
Purpose = Literal[
    "opentour.authority-journal.append-receipt.v1", "opentour.authority-journal.live-head.v1"
]


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate name")
        result[key] = value
    return result


def _number_rejected(value: str) -> None:
    raise ValueError("unsupported number")


def strict_json(data: bytes, *, limit: int = MAX_WIRE_BYTES) -> object:
    """All profile numbers are exact integers; floats/exponents are not coerced."""
    try:
        if type(data) is not bytes or len(data) > limit:
            raise ValueError("wire bound")
        value: object = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_float=_number_rejected,
            parse_constant=_number_rejected,
        )
        _unicode(value)
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise JournalError("SCHEMA_INVALID", 422) from None


def _unicode(value: object) -> None:
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
    elif isinstance(value, dict):
        for key, item in value.items():
            _unicode(key)
            _unicode(item)
    elif isinstance(value, list):
        for item in value:
            _unicode(item)


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    parse_byte_limit: ClassVar[int] = MAX_WIRE_BYTES

    @classmethod
    def parse(cls, data: bytes) -> Self:
        try:
            return cls.model_validate(strict_json(data, limit=cls.parse_byte_limit))
        except ValidationError:
            raise JournalError("SCHEMA_INVALID", 422) from None

    def wire(self) -> dict[str, object]:
        # Optional common ArtifactRef.expires_at preserves absent versus explicit null.
        return self.model_dump(mode="json", exclude_unset=True)


class ImmutableMetadataRef(WireModel):
    record_id: Id
    revision: Id
    sha256: Sha256


class MetadataRef(WireModel):
    artifact_id: Id
    kind: Annotated[str, Field(strict=True, min_length=1)]
    revision: Id
    sha256: Sha256
    manifest_id: Id
    environment_id: Id
    classification: Literal["control_metadata"]
    purpose_grant_id: Id
    expires_at: Timestamp | None = None


class JournalIdentity(WireModel):
    audit_account_id: Annotated[str, Field(strict=True, pattern=r"^[0-9]{12}$")]
    region: Annotated[str, Field(strict=True, pattern=r"^[a-z]{2}(?:-[a-z]+)+-[0-9]+$")]
    journal_instance_id: Id
    configuration_digest: Sha256


class HeadState(WireModel):
    organization_id: Id
    sequence: Counter
    chain_sha256: Sha256
    latest_epoch: Counter
    latest_authority_revision: Id
    unresolved_operation_id: Id | None
    revision: Revision


class AppendIntent(WireModel):
    organization_id: Id
    operation_id: Id
    expected_head_sequence: Counter
    expected_head_digest: Sha256
    mutation_kind: MutationKind
    proposed_epoch: Counter
    proposed_authority_revision: Id
    canonical_intent_digest: Sha256
    scoped_intent_ref: ImmutableMetadataRef
    actor_ref: ImmutableMetadataRef
    created_at: Timestamp


class AppendOutcome(WireModel):
    organization_id: Id
    operation_id: Id
    expected_head_sequence: Counter
    expected_head_digest: Sha256
    intent_sequence: PositiveCounter
    result: Literal["committed", "aborted"]
    control_transaction_ref: ImmutableMetadataRef
    resulting_epoch: Counter
    resulting_authority_revision: Id


class AppendReceiptPayload(WireModel):
    profile_version: Literal["1.0"]
    journal: JournalIdentity
    organization_id: Id
    operation_id: Id
    phase: Phase
    sequence: PositiveCounter
    previous_chain_sha256: Sha256
    chain_sha256: Sha256
    content_sha256: Sha256
    content_bucket_ref: Id
    content_key: Annotated[str, Field(strict=True, min_length=1, max_length=1024)]
    content_version_id: Id
    head_revision: Revision


class LiveHeadPayload(WireModel):
    profile_version: Literal["1.0"]
    journal: JournalIdentity
    request_nonce: Sha256
    observed_at: Timestamp
    head: HeadState


class JournalSignature(WireModel):
    profile_version: Literal["1.0"]
    algorithm: Literal["ES256"]
    encoding: Literal["base64url_unpadded_r_s_64"]
    purpose: Purpose
    key_id: Id
    key_registry_revision: Id
    signer_role: Literal["audit_journal_issuer"]
    payload_sha256: Sha256
    transcript_sha256: Sha256
    signature: Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_-]{86}$")]


class AppendReceiptEnvelope(WireModel):
    payload: AppendReceiptPayload
    signature: JournalSignature

    @model_validator(mode="after")
    def purpose_matches(self) -> Self:
        if self.signature.purpose != APPEND_PURPOSE:
            raise ValueError("signature purpose")
        return self


class LiveHeadEnvelope(WireModel):
    payload: LiveHeadPayload
    signature: JournalSignature

    @model_validator(mode="after")
    def purpose_matches(self) -> Self:
        if self.signature.purpose != HEAD_PURPOSE:
            raise ValueError("signature purpose")
        return self


class AppendResponse(WireModel):
    organization_id: Id
    operation_id: Id
    phase: Phase
    sequence: PositiveCounter
    chain_sha256: Sha256
    content_sha256: Sha256
    content_version_id: Id
    head_revision: Revision
    signed_receipt: AppendReceiptEnvelope


class JournalEntry(AppendReceiptPayload):
    signed_receipt: AppendReceiptEnvelope
    created_at: Timestamp
    signer_id: Id


class ReplayCheckpoint(WireModel):
    journal: JournalIdentity
    organization_id: Id
    sequence: Counter
    chain_sha256: Sha256
    latest_epoch: Counter
    latest_authority_revision: Id
    evidence_ref: MetadataRef


class VerifiedFrontierProof(WireModel):
    profile_version: Literal["1.0"]
    journal: JournalIdentity
    organization_id: Id
    checkpoint: ReplayCheckpoint
    live_head_envelope: LiveHeadEnvelope
    replay_manifest_ref: MetadataRef
    verified_entry_count: Counter
    unresolved_operation_id: Id | None
    latest_committed_epoch: Counter
    latest_committed_authority_revision: Id


class ReplayObservation(WireModel):
    entry: JournalEntry
    body: AppendIntent | AppendOutcome


class ReplayManifest(WireModel):
    parse_byte_limit: ClassVar[int] = MAX_MANIFEST_BYTES
    profile_version: Literal["1.0"]
    journal: JournalIdentity
    organization_id: Id
    checkpoint_sequence: Counter
    head_sequence: Counter
    observations: Annotated[tuple[ReplayObservation, ...], Field(max_length=MAX_REPLAY_ENTRIES)]

    @field_validator("observations", mode="before")
    @classmethod
    def immutable_array(cls, value: object) -> object:
        # JSON has arrays; freeze this collection without coercing any element.
        return tuple(value) if isinstance(value, list) else value


class LiveHeadRequest(WireModel):
    organization_id: Id
    request_nonce: Sha256


class ExactOperationRequest(WireModel):
    organization_id: Id
    operation_id: Id
    phase: Phase


class ExactEntryRequest(WireModel):
    organization_id: Id
    sequence: PositiveCounter


class ExactOperationResponse(WireModel):
    organization_id: Id
    operation_id: Id
    phase: Phase
    signed_response: AppendResponse | None


class ExactEntryResponse(WireModel):
    entry: JournalEntry
    body: AppendIntent | AppendOutcome
