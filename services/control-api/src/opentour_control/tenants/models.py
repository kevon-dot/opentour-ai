"""Closed organization setup values, immutable receipts, and safe state transitions."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from opentour_control.auth.config import identifier
from opentour_control.auth.session import VerifiedIdentityContext

MAX_REVISION = 9_223_372_036_854_775_807
Id = Annotated[str, Field(min_length=1, max_length=256)]
SetupState = Literal[
    "requested",
    "identity_provisioning",
    "identity_bound",
    "active",
    "provider_outcome_unknown",
    "action_required",
    "cancelled",
]
ProviderStage = Literal["organization", "membership", "activation"]
StageOutcome = Literal["not_dispatched", "dispatched", "unknown", "observed", "rejected"]
RemoteEffectState = Literal[
    "not_dispatched", "known_no_effect", "known_retained", "outcome_unknown"
]
SafeReason = Literal[
    "PROVIDER_OUTCOME_UNKNOWN",
    "PROVIDER_REJECTED",
    "PROVIDER_IDENTITY_CONFLICT",
    "PROVIDER_MEMBERSHIP_NOT_ACTIVE",
    "SETUP_CANCELLED",
]


class SetupError(Exception):
    def __init__(
        self,
        code: str,
        status: int,
        *,
        retryable: bool = False,
        current_revision: str | None = None,
        retry_after: int | None = None,
        field: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code, self.status, self.retryable = code, status, retryable
        self.current_revision, self.retry_after, self.field = current_revision, retry_after, field

    def wire(self, request_id: str) -> dict[str, object]:
        return {
            "code": self.code,
            "message": "Organization setup could not be completed.",
            "request_id": identifier(request_id),
            "retryable": self.retryable,
            "field_errors": []
            if self.field is None
            else [{"field": self.field, "code": self.code}],
            "current_revision": self.current_revision,
        }


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def validate_revision(value: str) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[1-9][0-9]{0,18}", value)
        or int(value) > MAX_REVISION
    ):
        raise ValueError("Invalid resource revision")
    return value


def advance_revision(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_REVISION:
        raise SetupError("SCHEMA_INVALID", 422)
    if value == MAX_REVISION:
        raise SetupError("REVISION_EXHAUSTED", 409, current_revision=str(value))
    return value + 1


def server_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SetupError("CLOCK_UNAVAILABLE", 503)
    return value.astimezone(UTC)


def timestamp(value: datetime) -> str:
    return server_time(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def validate_timestamp(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z", value
    ):
        raise ValueError("UTC timestamp required")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def validate_display_name(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Name must be text")
    # Check before trimming: a leading newline is not harmless surrounding whitespace.
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        raise ValueError("Control characters and unpaired surrogates are not allowed")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not 1 <= len(normalized) <= 120:
        raise ValueError("Name must contain between 1 and 120 characters")
    return normalized


class CreateRequest(ClosedModel):
    display_name: str
    accepted_terms_revision: Id

    _name = field_validator("display_name")(validate_display_name)

    @field_validator("accepted_terms_revision")
    @classmethod
    def safe_revision_id(cls, value: str) -> str:
        if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
            raise ValueError("Invalid terms revision")
        return value

    def digest(self) -> str:
        canonical = json.dumps(
            self.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


class RevisionRequest(ClosedModel):
    expected_revision: str

    _revision = field_validator("expected_revision")(validate_revision)


class CreateReceipt(ClosedModel):
    organization_id: Id
    operation_id: Id
    state: Literal["identity_provisioning"] = "identity_provisioning"
    revision: Literal["1"] = "1"
    request_id: Id


class OperationView(ClosedModel):
    organization_id: Id
    operation_id: Id
    state: SetupState
    revision: str
    request_id: Id
    retry_available: bool
    cancel_available: bool
    remote_effect_state: RemoteEffectState
    reason_code: SafeReason | None
    updated_at: str

    _revision = field_validator("revision")(validate_revision)
    _timestamp = field_validator("updated_at")(validate_timestamp)


class TermsView(ClosedModel):
    revision: Id
    document_url: Annotated[str, Field(max_length=2048)]
    effective_at: str

    @field_validator("document_url")
    @classmethod
    def approved_https(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or any(ord(c) < 33 for c in value)
        ):
            raise ValueError("Published terms require an approved HTTPS URL")
        return value

    _timestamp = field_validator("effective_at")(validate_timestamp)


@dataclass(frozen=True)
class Creator:
    issuer: str
    subject: str
    session_id: str
    session_generation: int

    @classmethod
    def from_verified(cls, value: VerifiedIdentityContext, expected_issuer: str) -> Creator:
        if (
            not isinstance(value, VerifiedIdentityContext)
            or value.actor.get("kind") != "human"
            or value.actor.get("issuer") != expected_issuer
        ):
            raise SetupError("IDENTITY_INVALID", 401)
        return cls(
            expected_issuer,
            identifier(value.actor["subject"]),
            identifier(value.session_id),
            value.session_generation,
        )

    def same_session(self, other: Creator) -> None:
        if self != other:
            raise SetupError("STALE_SESSION_CONTEXT", 409)


@dataclass(frozen=True)
class PublishedTerms:
    revision: str
    document_url: str
    effective_at: datetime

    def current_view(self, now: datetime) -> TermsView:
        if server_time(now) < server_time(self.effective_at):
            raise SetupError("TERMS_UNAVAILABLE", 503)
        try:
            return TermsView(
                revision=self.revision,
                document_url=self.document_url,
                effective_at=timestamp(self.effective_at),
            )
        except ValidationError as error:
            raise SetupError("TERMS_UNAVAILABLE", 503) from error


@dataclass(frozen=True)
class Operation:
    operation_id: str
    organization_id: str
    creator_issuer: str
    creator_subject: str
    display_name: str
    terms_revision: str
    correlation: str
    provider_environment: str
    state: SetupState
    revision: int
    stage: ProviderStage
    stage_outcome: StageOutcome
    provider_org_id: str | None
    provider_membership_id: str | None
    reason_code: SafeReason | None
    created_at: datetime
    updated_at: datetime

    def belongs_to(self, creator: Creator) -> None:
        if (self.creator_issuer, self.creator_subject) != (creator.issuer, creator.subject):
            raise SetupError("RESOURCE_NOT_FOUND", 404)

    def view(self, request_id: str) -> OperationView:
        if self.stage_outcome in {"dispatched", "unknown"}:
            remote: RemoteEffectState = "outcome_unknown"
        elif self.provider_org_id is not None:
            remote = "known_retained"
        elif self.stage_outcome == "rejected":
            remote = "known_no_effect"
        else:
            remote = "not_dispatched"
        return OperationView(
            organization_id=self.organization_id,
            operation_id=self.operation_id,
            state=self.state,
            revision=str(self.revision),
            request_id=request_id,
            retry_available=self.state
            in {"identity_provisioning", "identity_bound", "provider_outcome_unknown"},
            cancel_available=self.state not in {"active", "cancelled"},
            remote_effect_state=remote,
            reason_code=self.reason_code,
            updated_at=timestamp(self.updated_at),
        )


def correlation(organization_id: str, operation_id: str) -> str:
    material = json.dumps(
        ["opentour.organization-create.v1", organization_id, operation_id], separators=(",", ":")
    )
    return "ot_" + hashlib.sha256(material.encode()).hexdigest()[:60]


def key_digest(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or not value.isascii()
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise SetupError("SCHEMA_INVALID", 422, field="Idempotency-Key")
    return hashlib.sha256(value.encode()).hexdigest()


def decode_create(raw: bytes) -> CreateRequest:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON member")
            result[key] = value
        return result

    try:
        if len(raw) > 16_384:
            raise ValueError("Request too large")
        document = json.loads(raw, object_pairs_hook=unique)
        return CreateRequest.model_validate(document)
    except (ValueError, TypeError, UnicodeError, ValidationError):
        raise SetupError("SCHEMA_INVALID", 422) from None
