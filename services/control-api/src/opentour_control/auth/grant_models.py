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


APPROVED_CREATOR_POLICY_SHA256 = "0d293389f5e569c69e248368a92e08e445db8cc1d43c015304dc95bdf297769b"
INITIAL_CREATOR_REASON = "initial_owner_policy_installation"


@dataclass(frozen=True)
class CreatorActivation:
    event: str
    resource_kind: str
    expires_after_seconds: int | None = None


@dataclass(frozen=True)
class CreatorEntitlement:
    """One group of capability ceilings with identical installation restrictions."""

    capabilities: tuple[str, ...]
    template_id: str
    activations: tuple[CreatorActivation, ...]
    operation_purpose: str
    data_classes: tuple[DataClass, ...]
    requires_data_use_policy: bool


_ORGANIZATION_ACTIVATED = CreatorActivation("organization_activated", "organization")
_WORKSPACE_COMMITTED = CreatorActivation("workspace_committed", "workspace")
_PROJECT_COMMITTED = CreatorActivation("project_committed", "project")
_ENVIRONMENT_COMMITTED = CreatorActivation("environment_committed", "environment")
_ARTIFACT_COMMITTED = CreatorActivation("artifact_committed", "artifact")
_NOTE_WRITE_ADMITTED = CreatorActivation("note_write_intent_admitted", "support_note_write", 120)
_NOTE_READ_ADMITTED = CreatorActivation("note_read_intent_admitted", "support_note_read", 120)


# Lossless projection of the materialization fields of the 28 reviewed groups.
# The complete approved policy and domain contracts supply the current guards.
APPROVED_CREATOR_ENTITLEMENTS = (
    CreatorEntitlement(
        capabilities=(
            "members.manage",
            "access.manage",
            "members.invite",
            "workspace.create",
        ),
        template_id="owner-administration-v1",
        activations=(_ORGANIZATION_ACTIVATED,),
        operation_purpose="organization_administration",
        data_classes=("control_metadata",),
        requires_data_use_policy=False,
    ),
    CreatorEntitlement(
        capabilities=(
            "workspace.read",
            "workspace.manage",
            "project.create",
        ),
        template_id="workspace-creator-v1",
        activations=(_WORKSPACE_COMMITTED,),
        operation_purpose="workspace_administration",
        data_classes=("control_metadata",),
        requires_data_use_policy=False,
    ),
    CreatorEntitlement(
        capabilities=(
            "project.read",
            "project.manage",
        ),
        template_id="project-creator-v1",
        activations=(_PROJECT_COMMITTED,),
        operation_purpose="project_administration",
        data_classes=("control_metadata",),
        requires_data_use_policy=False,
    ),
    CreatorEntitlement(
        capabilities=(
            "billing.read",
            "billing.write",
            "quotas.manage",
        ),
        template_id="owner-commercial-v1",
        activations=(_ORGANIZATION_ACTIVATED,),
        operation_purpose="billing_administration",
        data_classes=("control_metadata",),
        requires_data_use_policy=False,
    ),
    CreatorEntitlement(
        capabilities=(
            "connections.manage",
            "credentials.manage",
            "integrations.manage",
            "readiness.manage",
            "environments.manage",
            "environment.health.read",
        ),
        template_id="owner-setup-v1",
        activations=(_ENVIRONMENT_COMMITTED, _WORKSPACE_COMMITTED),
        operation_purpose="environment_configuration",
        data_classes=("control_metadata",),
        requires_data_use_policy=False,
    ),
    CreatorEntitlement(
        capabilities=(
            "workflow.read",
            "workflow.write",
            "workflow.activate",
        ),
        template_id="owner-workflow-v1",
        activations=(_PROJECT_COMMITTED,),
        operation_purpose="workflow_development",
        data_classes=("control_metadata", "customer_confidential", "model_artifact"),
        requires_data_use_policy=True,
    ),
    CreatorEntitlement(
        capabilities=(
            "evidence.read",
            "evidence.reconstruct",
            "lineage.read",
            "lineage.write",
            "lineage.repair",
        ),
        template_id="owner-workflow-v1",
        activations=(_ARTIFACT_COMMITTED, _PROJECT_COMMITTED),
        operation_purpose="evidence_review",
        data_classes=("control_metadata", "customer_confidential", "model_artifact"),
        requires_data_use_policy=True,
    ),
    CreatorEntitlement(
        capabilities=(
            "labels.run",
            "labels.read",
            "labels.correct",
            "labels.qualify",
        ),
        template_id="owner-workflow-v1",
        activations=(_PROJECT_COMMITTED,),
        operation_purpose="data_labeling",
        data_classes=("control_metadata", "customer_confidential"),
        requires_data_use_policy=True,
    ),
    CreatorEntitlement(
        capabilities=(
            "scopes.discover",
            "scopes.read",
            "scopes.confirm",
        ),
        template_id="owner-experiment-v1",
        activations=(_PROJECT_COMMITTED,),
        operation_purpose="experiment_development",
        data_classes=("control_metadata", "customer_confidential"),
        requires_data_use_policy=True,
    ),
    CreatorEntitlement(
        capabilities=(
            "experiments.plan",
            "experiments.run",
            "experiments.freeze",
            "experiments.read",
        ),
        template_id="owner-experiment-v1",
        activations=(_PROJECT_COMMITTED,),
        operation_purpose="experiment_development",
        data_classes=("control_metadata", "customer_confidential"),
        requires_data_use_policy=True,
    ),
    CreatorEntitlement(
        capabilities=(
            "datasets.write",
            "datasets.validate",
            "datasets.seal",
            "datasets.read",
            "data.manage",
        ),
        template_id="owner-data-v1",
        activations=(_ARTIFACT_COMMITTED, _PROJECT_COMMITTED),
        operation_purpose="data_development",
        data_classes=("control_metadata", "customer_confidential"),
        requires_data_use_policy=True,
    ),
    CreatorEntitlement(
        capabilities=(
            "datasets.export",
            "data.export",
        ),
        template_id="owner-data-v1",
        activations=(_ARTIFACT_COMMITTED, _PROJECT_COMMITTED),
        operation_purpose="authorized_data_export",
        data_classes=("customer_confidential",),
        requires_data_use_policy=True,
    ),
    CreatorEntitlement(
        capabilities=(
            "synthetic.plan",
            "synthetic.run",
            "synthetic.read",
        ),
        template_id="owner-data-v1",
        activations=(_ARTIFACT_COMMITTED, _PROJECT_COMMITTED),
        operation_purpose="data_development",
        data_classes=("control_metadata", "customer_confidential"),
        requires_data_use_policy=True,
    ),
    CreatorEntitlement(
        capabilities=(
            "training.plan",
            "training.validate",
            "training.run",
            "training.read",
            "training.cancel",
        ),
        template_id="owner-training-v1",
        activations=(_ARTIFACT_COMMITTED, _PROJECT_COMMITTED),
        operation_purpose="model_training",
        data_classes=("control_metadata", "customer_confidential", "model_artifact"),
        requires_data_use_policy=True,
    ),
    CreatorEntitlement(
        capabilities=(
            "jobs.cancel",
            "jobs.diagnostics.read",
        ),
        template_id="owner-training-v1",
        activations=(_PROJECT_COMMITTED,),
        operation_purpose="job_management",
        data_classes=("control_metadata",),
        requires_data_use_policy=False,
    ),
    CreatorEntitlement(
        capabilities=("jobs.retry",),
        template_id="owner-training-v1",
        activations=(_PROJECT_COMMITTED,),
        operation_purpose="job_retry",
        data_classes=("control_metadata",),
        requires_data_use_policy=False,
    ),
    CreatorEntitlement(
        capabilities=(
            "benchmarks.plan",
            "benchmarks.validate",
            "benchmarks.lock",
            "benchmarks.read",
            "benchmarks.audit",
            "evaluations.plan",
            "evaluations.run",
            "evaluations.read",
            "evaluations.replay",
        ),
        template_id="owner-evaluation-request-v1",
        activations=(_PROJECT_COMMITTED,),
        operation_purpose="evaluation_request",
        data_classes=("control_metadata",),
        requires_data_use_policy=False,
    ),
    CreatorEntitlement(
        capabilities=(
            "changes.write",
            "changes.plan",
            "changes.read",
            "artifacts.write",
            "artifacts.qualify",
            "deployments.authorize",
            "deployments.promote",
            "deployments.rollback",
        ),
        template_id="owner-release-v1",
        activations=(_ARTIFACT_COMMITTED, _ENVIRONMENT_COMMITTED, _PROJECT_COMMITTED),
        operation_purpose="model_release",
        data_classes=("control_metadata", "model_artifact"),
        requires_data_use_policy=True,
    ),
    CreatorEntitlement(
        capabilities=("changes.execute",),
        template_id="owner-release-v1",
        activations=(_ARTIFACT_COMMITTED, _ENVIRONMENT_COMMITTED, _PROJECT_COMMITTED),
        operation_purpose="model_release",
        data_classes=("control_metadata", "model_artifact"),
        requires_data_use_policy=True,
    ),
    CreatorEntitlement(
        capabilities=(
            "monitoring.manage",
            "releases.read",
        ),
        template_id="owner-release-v1",
        activations=(_ARTIFACT_COMMITTED, _ENVIRONMENT_COMMITTED, _PROJECT_COMMITTED),
        operation_purpose="model_release",
        data_classes=("control_metadata", "model_artifact"),
        requires_data_use_policy=True,
    ),
    CreatorEntitlement(
        capabilities=(
            "value.read",
            "value.write",
        ),
        template_id="owner-delivery-v1",
        activations=(_ARTIFACT_COMMITTED, _PROJECT_COMMITTED),
        operation_purpose="customer_delivery",
        data_classes=("control_metadata",),
        requires_data_use_policy=False,
    ),
    CreatorEntitlement(
        capabilities=(
            "packages.create",
            "packages.read",
            "packages.export",
        ),
        template_id="owner-delivery-v1",
        activations=(_ARTIFACT_COMMITTED, _PROJECT_COMMITTED),
        operation_purpose="customer_delivery",
        data_classes=("control_metadata", "customer_confidential", "model_artifact"),
        requires_data_use_policy=True,
    ),
    CreatorEntitlement(
        capabilities=(
            "incident.read",
            "incident.manage",
        ),
        template_id="owner-incident-v1",
        activations=(_PROJECT_COMMITTED,),
        operation_purpose="incident_management",
        data_classes=("control_metadata",),
        requires_data_use_policy=False,
    ),
    CreatorEntitlement(
        capabilities=("incident.resolve",),
        template_id="owner-incident-v1",
        activations=(_PROJECT_COMMITTED,),
        operation_purpose="incident_management",
        data_classes=("control_metadata",),
        requires_data_use_policy=False,
    ),
    CreatorEntitlement(
        capabilities=(
            "support.create",
            "support.read",
            "support.manage",
            "support.close",
            "support.delegate",
        ),
        template_id="owner-customer-support-v1",
        activations=(_ORGANIZATION_ACTIVATED, _WORKSPACE_COMMITTED),
        operation_purpose="customer_support",
        data_classes=("control_metadata",),
        requires_data_use_policy=False,
    ),
    CreatorEntitlement(
        capabilities=("support.note.create",),
        template_id="owner-customer-support-v1",
        activations=(_NOTE_WRITE_ADMITTED,),
        operation_purpose="support_case_note",
        data_classes=("customer_confidential",),
        requires_data_use_policy=True,
    ),
    CreatorEntitlement(
        capabilities=("support.note.read",),
        template_id="owner-customer-support-v1",
        activations=(_NOTE_READ_ADMITTED,),
        operation_purpose="support_case_note",
        data_classes=("customer_confidential",),
        requires_data_use_policy=True,
    ),
    CreatorEntitlement(
        capabilities=(
            "policy.authorize",
            "source.authorize",
            "model_rights.authorize",
            "budget.authorize",
        ),
        template_id="owner-customer-approval-v1",
        activations=(
            _ORGANIZATION_ACTIVATED,
            _WORKSPACE_COMMITTED,
            _PROJECT_COMMITTED,
            _ENVIRONMENT_COMMITTED,
        ),
        operation_purpose="customer_policy_approval",
        data_classes=("control_metadata",),
        requires_data_use_policy=False,
    ),
)
_BIRTH_ENTITLEMENTS = tuple(
    entitlement
    for entitlement in APPROVED_CREATOR_ENTITLEMENTS
    if _ORGANIZATION_ACTIVATED in entitlement.activations
)
_BIRTH_TEMPLATE_IDS = frozenset(entitlement.template_id for entitlement in _BIRTH_ENTITLEMENTS)
_CREATOR_TEMPLATE_IDS = frozenset(
    entitlement.template_id for entitlement in APPROVED_CREATOR_ENTITLEMENTS
)


class InitialAuthorityPolicy(ClosedModel):
    """The configured immutable policy binding used by the trusted installer."""

    policy_ref: ImmutableMetadataRef
    template_bindings: Annotated[
        tuple[TemplateBinding, ...], BeforeValidator(_array), Field(min_length=1, max_length=15)
    ]
    authenticated_installer: ImmutableMetadataRef
    enrolled_organization_id: Id
    enrollment_ref: ImmutableMetadataRef

    @model_validator(mode="after")
    def approved_binding(self) -> Self:
        template_ids = {binding.template_id for binding in self.template_bindings}
        if (
            self.policy_ref.sha256 != APPROVED_CREATOR_POLICY_SHA256
            or len(template_ids) != len(self.template_bindings)
            or not _BIRTH_TEMPLATE_IDS <= template_ids <= _CREATOR_TEMPLATE_IDS
        ):
            raise ValueError("Unapproved creator policy binding")
        return self

    def birth_grants(
        self,
        organization_id: str,
        subject: str,
        operation_id: str,
        authority_revision: str,
        now: datetime,
    ) -> tuple[AccessGrant, ...]:
        """Compile prepared organization grants for a verified local creator."""

        if organization_id != self.enrolled_organization_id:
            raise GrantError("CREATOR_POLICY_UNAVAILABLE", 503)
        scope = ResourceScope(
            organization_id=organization_id,
            workspace_ids=(),
            project_ids=(),
            environment_ids=(),
            artifact_refs=(),
            support_note_scopes=(),
        )
        principal = Principal(type="user", id=subject)
        valid_from = server_time(now).isoformat().replace("+00:00", "Z")
        bindings = {binding.template_id: binding for binding in self.template_bindings}
        grants = []
        for entitlement in _BIRTH_ENTITLEMENTS:
            if entitlement.requires_data_use_policy or entitlement.data_classes != (
                "control_metadata",
            ):
                raise GrantError("CREATOR_POLICY_UNAVAILABLE", 503)
            binding = bindings[entitlement.template_id]
            for capability in entitlement.capabilities:
                grant_id = sha256(
                    canonical(
                        {
                            "policy_ref": self.policy_ref.wire(),
                            "enrollment_ref": self.enrollment_ref.wire(),
                            "organization_id": organization_id,
                            "principal": principal.wire(),
                            "operation_id": operation_id,
                            "capability": capability,
                            "template": binding.wire(),
                        }
                    )
                )
                grants.append(
                    AccessGrant(
                        grant_id=f"grant-{grant_id}",
                        revision="1",
                        principal=principal,
                        capabilities=(capability,),
                        resource_scope=scope,
                        purposes=(entitlement.operation_purpose,),
                        data_classes=entitlement.data_classes,
                        valid_from=valid_from,
                        expires_at=None,
                        reason_code=INITIAL_CREATOR_REASON,
                        state="prepared",
                        parent_grant_ids=(),
                        template_bindings=(binding,),
                        creation_operation_id=operation_id,
                        creation_authority_revision=authority_revision,
                    )
                )
        return tuple(grants)


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
