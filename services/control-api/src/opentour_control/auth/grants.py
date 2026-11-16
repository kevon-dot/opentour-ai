"""Resource-scoped capability and delegation decisions.

Trusted ports supply current domain facts. These decisions are a prerequisite,
not a cell lease, provider authorization, or proof of a committed grant mutation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from .grant_models import (
    ACTION_CAPABILITIES,
    AccessGrant,
    ArtifactRef,
    CreateGrant,
    DataClass,
    GrantError,
    GrantVocabulary,
    InputResourceScope,
    NoteScope,
    Principal,
    TemplateBinding,
    instant,
    server_time,
)
from .session import AuthContext

ResourceKind = Literal["organization", "workspace", "project", "environment", "artifact", "note"]


@dataclass(frozen=True)
class ResourceRecord:
    """Current canonical owner/parent/lifecycle projection from the owning domain.

    Domain composition must hold its admission fence for sensitive commits; a
    returned snapshot is not a lease. A caller-supplied object is never a source.
    """

    kind: ResourceKind
    resource_id: str
    organization_id: str
    revision: str
    state: Literal["active", "archived", "removed", "reconciliation_required"]
    workspace_id: str | None
    project_id: str | None
    environment_id: str | None
    artifact_ref: ArtifactRef | None = None
    note_scope: NoteScope | None = None


class ResourceOwnershipPort(Protocol):
    def current(
        self, organization_id: str, kind: ResourceKind, resource_id: str
    ) -> ResourceRecord | None: ...


@dataclass(frozen=True)
class PrincipalRecord:
    """Trusted current identity/workload registration, never provider role text."""

    principal: Principal
    organization_id: str
    state: Literal["active", "removed", "reconciliation_required"]
    duty: Literal["human", "trainer", "evaluator", "deployer", "service", "support"]
    registration_revision: str
    identity_issuer: str
    identity_subject: str


class PrincipalPort(Protocol):
    def current(self, organization_id: str, principal: Principal) -> PrincipalRecord | None: ...


@dataclass(frozen=True)
class AuthorityState:
    """Server-read SQL state after the coordinator's independent frontier check."""

    organization_id: str
    authority_revision: str
    revocation_epoch: int
    state: Literal["current", "reconciliation_required", "unavailable"]
    current_operation_id: str | None

    def require_current(self, context: AuthContext) -> None:
        if context.organization_id != self.organization_id:
            raise GrantError("RESOURCE_NOT_FOUND", 404)
        if self.state != "current" or self.current_operation_id is not None:
            raise GrantError("AUTHORITY_UNAVAILABLE", 503, retryable=True)
        if (context.authority_revision, context.revocation_epoch) != (
            self.authority_revision,
            self.revocation_epoch,
        ):
            raise GrantError("STALE_AUTHORITY", 409)


@dataclass(frozen=True)
class Decision:
    grant_ids: tuple[str, ...]
    authority_revision: str
    revocation_epoch: int


@dataclass(frozen=True)
class DelegationPlan:
    parent_grant_ids: tuple[str, ...]
    resource_revisions: tuple[tuple[str, str, str], ...]
    principal_revision: str


def _duty(
    principal: PrincipalRecord, capabilities: tuple[str, ...], classes: tuple[str, ...]
) -> None:
    if principal.state != "active":
        raise GrantError("PRINCIPAL_UNAVAILABLE", 403)
    if principal.principal.type == "user" and principal.duty != "human":
        raise GrantError("PRINCIPAL_UNAVAILABLE", 403)
    if principal.principal.type == "support" and principal.duty != "support":
        raise GrantError("PRINCIPAL_UNAVAILABLE", 403)
    if principal.principal.type == "workload" and principal.duty in {"human", "support"}:
        raise GrantError("PRINCIPAL_UNAVAILABLE", 403)
    if principal.duty == "trainer" and (
        "protected_evaluation" in classes
        or any(c.startswith("deployments.") for c in capabilities)
        or "evaluations.inspect_protected" in capabilities
        or "benchmarks.disclose" in capabilities
    ):
        raise GrantError("DUTY_CONFLICT", 403)


def _valid_chain(
    grant: AccessGrant,
    grants: Mapping[str, AccessGrant],
    now: datetime,
    organization_id: str,
    seen: frozenset[str] = frozenset(),
) -> bool:
    # Bounded traversal fails closed on missing, cyclic, foreign or inactive ancestry.
    if (
        grant.grant_id in seen
        or len(seen) >= 64
        or grant.resource_scope.organization_id != organization_id
        or not grant.current(now)
    ):
        return False
    parents = [grants[p] for p in grant.parent_grant_ids if p in grants]
    if grant.parent_grant_ids and any(
        not any(
            capability in parent.capabilities
            and grant.resource_scope.within(parent.resource_scope)
            and set(grant.purposes) <= set(parent.purposes)
            and set(grant.data_classes) <= set(parent.data_classes)
            and instant(parent.valid_from) <= instant(grant.valid_from)
            and (
                parent.expires_at is None
                or (
                    grant.expires_at is not None
                    and instant(grant.expires_at) <= instant(parent.expires_at)
                )
            )
            for parent in parents
        )
        for capability in grant.capabilities
    ):
        return False
    return all(
        parent_id in grants
        and _valid_chain(grants[parent_id], grants, now, organization_id, seen | {grant.grant_id})
        for parent_id in grant.parent_grant_ids
    )


def _matches(scope: InputResourceScope, resource: ResourceRecord) -> bool:
    if resource.kind == "organization":
        # The organization is server-derived; this does not match any child branch.
        return scope.organization_only() and resource.organization_id == resource.resource_id
    if resource.kind == "workspace":
        return resource.resource_id in scope.workspace_ids
    if resource.kind == "project":
        return resource.resource_id in scope.project_ids
    if resource.kind == "environment":
        return resource.resource_id in scope.environment_ids
    if resource.kind == "artifact":
        return resource.artifact_ref is not None and resource.artifact_ref in scope.artifact_refs
    return resource.note_scope is not None and resource.note_scope in scope.support_note_scopes


class GrantResolver:
    def __init__(
        self,
        resources: ResourceOwnershipPort,
        principals: PrincipalPort,
        vocabulary: GrantVocabulary,
        clock: Callable[[], datetime],
    ) -> None:
        self.resources, self.principals, self.vocabulary = resources, principals, vocabulary
        self.clock = clock

    def _now(self, started: datetime) -> datetime:
        current = server_time(self.clock())
        if current < started:
            raise GrantError("CLOCK_UNAVAILABLE", 503)
        return current

    def principal(self, organization_id: str, requested: Principal) -> PrincipalRecord:
        record = self.principals.current(organization_id, requested)
        if (
            record is None
            or record.organization_id != organization_id
            or record.principal != requested
        ):
            raise GrantError("RESOURCE_NOT_FOUND", 404)
        return record

    def resource(
        self, organization_id: str, kind: ResourceKind, resource_id: str
    ) -> ResourceRecord:
        value = self.resources.current(organization_id, kind, resource_id)
        if value is None or (value.organization_id, value.kind, value.resource_id) != (
            organization_id,
            kind,
            resource_id,
        ):
            raise GrantError("RESOURCE_NOT_FOUND", 404)
        if value.state != "active":
            raise GrantError("RESOURCE_UNAVAILABLE", 409)
        # Re-read parent lifecycle. A project cannot outlive a suspended workspace.
        parents: list[tuple[ResourceKind, str]] = []
        if value.workspace_id is not None and kind != "workspace":
            parents.append(("workspace", value.workspace_id))
        if value.project_id is not None and kind not in {"workspace", "project"}:
            parents.append(("project", value.project_id))
        if value.environment_id is not None and kind in {"artifact", "note"}:
            parents.append(("environment", value.environment_id))
        for parent_kind, parent_id in parents:
            parent = self.resources.current(organization_id, parent_kind, parent_id)
            if parent is None or (parent.organization_id, parent.kind, parent.resource_id) != (
                organization_id,
                parent_kind,
                parent_id,
            ):
                raise GrantError("RESOURCE_NOT_FOUND", 404)
            if parent.state != "active":
                raise GrantError("RESOURCE_UNAVAILABLE", 409)
            if (parent_kind == "project" and parent.workspace_id != value.workspace_id) or (
                parent_kind == "environment"
                and (
                    parent.workspace_id != value.workspace_id
                    or parent.project_id != value.project_id
                )
            ):
                raise GrantError("RESOURCE_NOT_FOUND", 404)
        return value

    def decide(
        self,
        context: AuthContext,
        principal: PrincipalRecord,
        authority: AuthorityState,
        grants: Mapping[str, AccessGrant],
        *,
        action: str,
        resource_kind: ResourceKind,
        resource_id: str,
        purpose: str,
        data_class: DataClass,
        now: datetime,
    ) -> Decision:
        now = server_time(now)
        authority.require_current(context)
        capability = ACTION_CAPABILITIES.get(action)
        if capability is None or purpose not in self.vocabulary.purpose_codes:
            raise GrantError("UNKNOWN_ACTION", 403)
        if principal.organization_id != context.organization_id:
            raise GrantError("RESOURCE_NOT_FOUND", 404)
        if (principal.identity_issuer, principal.identity_subject) != (
            context.identity.actor["issuer"],
            context.identity.actor["subject"],
        ):
            raise GrantError("PRINCIPAL_UNAVAILABLE", 403)
        _duty(principal, (capability,), (data_class,))
        resource = self.resource(context.organization_id, resource_kind, resource_id)
        now = self._now(now)
        if resource.artifact_ref is not None and (
            resource.artifact_ref.expires_at is not None
            and now >= instant(resource.artifact_ref.expires_at)
        ):
            raise GrantError("RESOURCE_UNAVAILABLE", 409)
        matches = tuple(
            sorted(
                grant.grant_id
                for grant in grants.values()
                if grant.principal == principal.principal
                and capability in grant.capabilities
                and purpose in grant.purposes
                and data_class in grant.data_classes
                and _matches(grant.resource_scope, resource)
                and _valid_chain(grant, grants, now, context.organization_id)
            )
        )
        if not matches:
            raise GrantError("CAPABILITY_DENIED", 403)
        return Decision(matches, authority.authority_revision, authority.revocation_epoch)

    def scope(
        self, organization_id: str, workspace_id: str, body: InputResourceScope
    ) -> tuple[tuple[str, str, str], ...]:
        records = [self.resource(organization_id, "workspace", workspace_id)]
        for kind, ids in (
            ("workspace", body.workspace_ids),
            ("project", body.project_ids),
            ("environment", body.environment_ids),
        ):
            for resource_id in ids:
                record = self.resource(organization_id, kind, resource_id)  # type: ignore[arg-type]
                if (kind == "workspace" and record.resource_id != workspace_id) or (
                    kind != "workspace" and record.workspace_id != workspace_id
                ):
                    raise GrantError("RESOURCE_NOT_FOUND", 404)
                records.append(record)
        for ref in body.artifact_refs:
            record = self.resource(organization_id, "artifact", ref.artifact_id)
            if record.workspace_id != workspace_id or record.artifact_ref != ref:
                raise GrantError("RESOURCE_NOT_FOUND", 404)
            records.append(record)
        for scope in body.support_note_scopes:
            record_id = (
                scope.write_intent_id
                if scope.resource_type == "support_note_write"
                else scope.note_ref.note_id
            )
            record = self.resource(organization_id, "note", record_id)
            if record.workspace_id != workspace_id or record.note_scope != scope:
                raise GrantError("RESOURCE_NOT_FOUND", 404)
            records.append(record)
        return tuple(sorted({(r.kind, r.resource_id, r.revision) for r in records}))

    def delegation(
        self,
        context: AuthContext,
        actor: PrincipalRecord,
        authority: AuthorityState,
        grants: Mapping[str, AccessGrant],
        *,
        workspace_id: str,
        body: CreateGrant,
        management_purpose: str,
        now: datetime,
        templates: tuple[TemplateBinding, ...] = (),
    ) -> DelegationPlan:
        now = server_time(now)
        authority.require_current(context)
        if body.expected_authority_revision != authority.authority_revision:
            raise GrantError("STALE_AUTHORITY", 409)
        self.vocabulary.validate(body, templates)
        self.decide(
            context,
            actor,
            authority,
            grants,
            action="access.manage",
            resource_kind="workspace",
            resource_id=workspace_id,
            purpose=management_purpose,
            data_class="control_metadata",
            now=now,
        )
        subject = self.principal(context.organization_id, body.principal)
        _duty(subject, body.capabilities, body.data_classes)
        revisions = self.scope(context.organization_id, workspace_id, body.resource_scope)
        now = self._now(now)
        if body.expires_at is not None and instant(body.expires_at) <= now:
            raise GrantError("GRANT_EXPIRED", 403)
        parents: set[str] = set()
        for capability in body.capabilities:
            # One parent must support the child's complete scope/purpose/class/validity
            # for this capability. Never construct a cross-product from unrelated grants.
            candidates = [
                g
                for g in grants.values()
                if (
                    g.principal == actor.principal
                    and capability in g.capabilities
                    and _valid_chain(g, grants, now, context.organization_id)
                    and body.resource_scope.within(g.resource_scope)
                    and set(body.purposes) <= set(g.purposes)
                    and set(body.data_classes) <= set(g.data_classes)
                    and instant(g.valid_from) <= instant(body.valid_from)
                    and (
                        g.expires_at is None
                        or (
                            body.expires_at is not None
                            and instant(body.expires_at) <= instant(g.expires_at)
                        )
                    )
                )
            ]
            if not candidates:
                raise GrantError("DELEGATION_CEILING", 403)
            parents.add(min(candidates, key=lambda g: g.grant_id).grant_id)
        return DelegationPlan(tuple(sorted(parents)), revisions, subject.registration_revision)
