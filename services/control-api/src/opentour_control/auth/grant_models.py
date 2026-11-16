"""Closed grant values and finite capability names; no role implies a permission.

Scope validation establishes shape. Authenticated resource ownership, current
identity, delegation and the independently verified journal remain separate gates.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from opentour_journal.crypto import canonical, sha256
from opentour_journal.models import (
    Counter,
    Id,
    ImmutableMetadataRef,
    JournalError,
    Revision,
    Sha256,
    strict_json,
)
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

DataClass = Literal[
    "control_metadata", "customer_confidential", "protected_evaluation", "model_artifact"
]

# Registered symbols from PRD01 and the current143 literal catalog. This is a
# vocabulary, never an initial-owner template or an automatic grant assignment.
CAPABILITIES = frozenset(
    """
    members.manage access.manage members.invite
    workspace.create workspace.read workspace.manage project.create project.read project.manage
    billing.read billing.write quotas.manage connections.manage credentials.manage
    integrations.manage
    readiness.manage environments.manage environment.health.read
    workflow.read workflow.write workflow.activate evidence.read evidence.reconstruct
    lineage.read lineage.write lineage.repair labels.run labels.read labels.correct labels.qualify
    scopes.discover scopes.read scopes.confirm
    experiments.plan experiments.run experiments.freeze experiments.read
    datasets.write datasets.validate datasets.seal datasets.read datasets.export
    synthetic.plan synthetic.run synthetic.read data.manage data.export
    training.plan training.validate training.run training.read training.cancel
    jobs.cancel jobs.retry jobs.diagnostics.read
    benchmarks.plan benchmarks.validate benchmarks.lock benchmarks.read benchmarks.audit
    evaluations.plan evaluations.run evaluations.read evaluations.replay
    changes.write changes.plan changes.read changes.execute
    artifacts.write artifacts.qualify deployments.authorize deployments.promote deployments.rollback
    monitoring.manage releases.read value.read value.write packages.create packages.read
    packages.export
    incident.read incident.manage incident.resolve
    support.create support.read support.manage support.close support.note.create support.note.read
    support.delegate evaluations.inspect_protected benchmarks.disclose incident.severity_review
    operations.read operations.execute support.emergency_request support.emergency_approve
    security.admin policy.authorize source.authorize model_rights.authorize budget.authorize
    """.split()
)
ACTION_CAPABILITIES = {
    **{name: name for name in CAPABILITIES},
    "scope.scan": "scopes.discover",
    "train": "training.run",
    "experiment.create": "experiments.plan",
    "connector.revalidate": "connections.manage",
}


class GrantError(Exception):
    """Safe public failure; provider/SQL exception text must never be attached."""

    def __init__(
        self,
        code: str,
        status: int = 403,
        *,
        retryable: bool = False,
        current_revision: str | None = None,
    ) -> None:
        self.code, self.status, self.retryable = code, status, retryable
        self.current_revision = current_revision
        super().__init__(code)

    def wire(self, request_id: str) -> dict[str, object]:
        return {
            "code": self.code,
            "message": "Grant request could not be completed.",
            "request_id": request_id,
            "retryable": self.retryable,
            "field_errors": [],
            "current_revision": self.current_revision,
        }


def server_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise GrantError("CLOCK_UNAVAILABLE", 503)
    return value.astimezone(UTC)


def validate_timestamp(value: str) -> str:
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z", value
    ):
        raise ValueError("UTC timestamp required")
    datetime.fromisoformat(value)
    return value


def instant(value: str) -> datetime:
    return datetime.fromisoformat(validate_timestamp(value))


def timestamp(value: datetime) -> str:
    return server_time(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _array(value: object) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("Array required")
    return tuple(value)


def _unique[T](value: tuple[T, ...]) -> tuple[T, ...]:
    if len(value) != len(set(value)):
        raise ValueError("Duplicate array item")
    return value


def registered_capability(value: str) -> str:
    if value not in CAPABILITIES:
        raise ValueError("Unregistered capability")
    return value


Timestamp = Annotated[str, Field(strict=True), AfterValidator(validate_timestamp)]
Capability = Annotated[Id, AfterValidator(registered_capability)]
Ids = Annotated[
    tuple[Id, ...], BeforeValidator(_array), Field(max_length=100), AfterValidator(_unique)
]
Capabilities = Annotated[
    tuple[Capability, ...],
    BeforeValidator(_array),
    Field(min_length=1, max_length=50),
    AfterValidator(_unique),
]
Classes = Annotated[
    tuple[DataClass, ...],
    BeforeValidator(_array),
    Field(max_length=4),
    AfterValidator(_unique),
]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    @classmethod
    def parse(cls, data: bytes) -> Self:
        try:
            return cls.model_validate(strict_json(data))
        except (ValidationError, ValueError, JournalError):
            raise GrantError("SCHEMA_INVALID", 422) from None

    def wire(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_unset=True)

    def digest(self) -> str:
        return sha256(canonical(self.wire()))


class Principal(ClosedModel):
    type: Literal["user", "workload", "support"]
    id: Id


class ArtifactRef(ClosedModel):
    """Typed representation of the exact common ArtifactRef contract."""

    artifact_id: Id
    kind: Annotated[str, Field(min_length=1)]
    revision: Id
    sha256: Sha256
    manifest_id: Id
    environment_id: Id
    classification: DataClass
    purpose_grant_id: Id
    expires_at: Timestamp | None = None


class SupportNoteRef(ClosedModel):
    environment_id: Id
    note_id: Id
    revision: Revision


class SupportNoteWrite(ClosedModel):
    resource_type: Literal["support_note_write"]
    case_id: Id
    environment_id: Id
    write_intent_id: Id
    max_utf8_bytes: Literal[16384]


class SupportNoteRead(ClosedModel):
    resource_type: Literal["support_note_read"]
    case_id: Id
    note_ref: SupportNoteRef


NoteScope = Annotated[SupportNoteWrite | SupportNoteRead, Field(discriminator="resource_type")]
ArtifactRefs = Annotated[
    tuple[ArtifactRef, ...],
    BeforeValidator(_array),
    Field(max_length=100),
    AfterValidator(_unique),
]
NoteScopes = Annotated[
    tuple[NoteScope, ...],
    BeforeValidator(_array),
    Field(max_length=100),
    AfterValidator(_unique),
]


class InputResourceScope(ClosedModel):
    workspace_ids: Ids
    project_ids: Ids
    environment_ids: Ids
    artifact_refs: ArtifactRefs
    support_note_scopes: NoteScopes

    def organization_only(self) -> bool:
        return not any(
            (
                self.workspace_ids,
                self.project_ids,
                self.environment_ids,
                self.artifact_refs,
                self.support_note_scopes,
            )
        )

    def within(self, parent: InputResourceScope) -> bool:
        # Dimension-by-dimension containment; empty never means wildcard.
        # A child resource grant cannot become an organization-level grant by
        # dropping every child ID. The approved organization template is explicit.
        if self.organization_only() and not parent.organization_only():
            return False
        return all(
            set(getattr(self, field)) <= set(getattr(parent, field))
            for field in (
                "workspace_ids",
                "project_ids",
                "environment_ids",
                "artifact_refs",
                "support_note_scopes",
            )
        )


class ResourceScope(InputResourceScope):
    organization_id: Id


class CreateGrant(ClosedModel):
    principal: Principal
    capabilities: Capabilities
    resource_scope: InputResourceScope
    purposes: Ids
    data_classes: Classes
    valid_from: Timestamp
    expires_at: Timestamp | None
    reason_code: Id
    expected_authority_revision: Id

    @model_validator(mode="after")
    def valid_interval(self) -> Self:
        if self.expires_at is None:
            if self.principal.type != "user":
                raise ValueError("Workload and support grants require expiry")
        elif instant(self.expires_at) <= instant(self.valid_from):
            raise ValueError("Empty validity interval")
        return self


class TemplateBinding(ClosedModel):
    template_id: Id
    revision: Revision
    sha256: Sha256


class AccessGrant(ClosedModel):
    grant_id: Id
    revision: Revision
    principal: Principal
    capabilities: Capabilities
    resource_scope: ResourceScope
    purposes: Ids
    data_classes: Classes
    valid_from: Timestamp
    expires_at: Timestamp | None
    reason_code: Id
    state: Literal["prepared", "active", "revoked"]
    parent_grant_ids: Ids
    template_bindings: Annotated[
        tuple[TemplateBinding, ...],
        BeforeValidator(_array),
        Field(max_length=50),
        AfterValidator(_unique),
    ]
    creation_operation_id: Id
    creation_authority_revision: Id

    @model_validator(mode="after")
    def valid_interval(self) -> Self:
        if self.expires_at is None and self.principal.type != "user":
            raise ValueError("Workload and support grants require expiry")
        if self.expires_at is not None and instant(self.expires_at) <= instant(self.valid_from):
            raise ValueError("Empty validity interval")
        if self.grant_id in self.parent_grant_ids:
            raise ValueError("Self ancestry")
        return self

    def current(self, now: datetime) -> bool:
        return (
            self.state == "active"
            and instant(self.valid_from) <= now
            and (self.expires_at is None or now < instant(self.expires_at))
        )

    def temporal_state(
        self, now: datetime
    ) -> Literal["not_yet_valid", "active", "expired", "revoked"]:
        """Temporal projection only; current authority and ancestry are separate."""
        now = server_time(now)
        if self.state == "prepared":
            raise GrantError("AUTHORITY_UNAVAILABLE", 503)
        if self.state == "revoked":
            return "revoked"
        if self.expires_at is not None and now >= instant(self.expires_at):
            return "expired"
        if now < instant(self.valid_from):
            return "not_yet_valid"
        return "active"


class GrantCreationReceipt(ClosedModel):
    grant_id: Id
    revision: Revision
    authority_revision: Id
    revocation_epoch: Counter
    committed_at: Timestamp


class GrantCurrentStatus(ClosedModel):
    grant_id: Id
    revision: Revision
    state: Literal["not_yet_valid", "active", "expired", "revoked"]
    authority_status: Literal["current", "reconciliation_required", "unavailable"]
    observed_at: Timestamp


class GrantOperationView(ClosedModel):
    operation_id: Id
    operation_kind: Literal["access_grant_create"]
    operation_revision: Revision
    grant_id: Id
    state: Literal["prepared", "reconciliation_required", "succeeded", "aborted"]
    creation_receipt: GrantCreationReceipt | None
    current_grant_status: GrantCurrentStatus | None
    current_status_unavailable_reason: (
        Literal["AUTHORITY_UNAVAILABLE", "RECONCILIATION_REQUIRED"] | None
    )
    request_id: Id

    @model_validator(mode="after")
    def consistent_state(self) -> Self:
        if self.state != "succeeded":
            if any(
                v is not None
                for v in (
                    self.creation_receipt,
                    self.current_grant_status,
                    self.current_status_unavailable_reason,
                )
            ):
                raise ValueError("No authority receipt before success")
        else:
            if self.creation_receipt is None or self.creation_receipt.grant_id != self.grant_id:
                raise ValueError("Missing or mismatched creation receipt")
            if self.current_grant_status is None:
                if self.current_status_unavailable_reason is None:
                    raise ValueError("Missing current status needs reason")
            elif (
                self.current_grant_status.grant_id != self.grant_id
                or self.current_status_unavailable_reason is not None
            ):
                raise ValueError("Mismatched current status")
        return self


class GrantTuple(ClosedModel):
    """One reviewed capability/scope/purpose/class tuple; never union dimensions."""

    capability: Capability
    resource_scope: ResourceScope
    purpose: Id
    data_class: DataClass
    valid_from: Timestamp
    expires_at: Timestamp | None


class InitialAuthorityPolicy(ClosedModel):
    """Required trusted installation input. No default launch policy is supplied."""

    policy_ref: ImmutableMetadataRef
    template: TemplateBinding
    authenticated_installer: ImmutableMetadataRef
    enrolled_organization_id: Id
    tuples: Annotated[
        tuple[GrantTuple, ...], BeforeValidator(_array), Field(min_length=1, max_length=100)
    ]
    allow_durable_human_expiry: bool

    @model_validator(mode="after")
    def same_organization(self) -> Self:
        if any(
            t.resource_scope.organization_id != self.enrolled_organization_id for t in self.tuples
        ):
            raise ValueError("Policy organization mismatch")
        return self


@dataclass(frozen=True)
class GrantVocabulary:
    """Server-installed finite codes; membership in these sets grants no right."""

    purpose_codes: frozenset[str]
    reason_codes: frozenset[str]
    allow_durable_human_expiry: Callable[[Principal, tuple[TemplateBinding, ...]], bool]

    def validate(self, body: CreateGrant, templates: tuple[TemplateBinding, ...] = ()) -> None:
        if (
            body.reason_code not in self.reason_codes
            or not set(body.purposes) <= self.purpose_codes
        ):
            raise GrantError("SCHEMA_INVALID", 422)
        if body.expires_at is None and not self.allow_durable_human_expiry(
            body.principal, templates
        ):
            raise GrantError("EXPIRY_POLICY_REQUIRED", 403)
