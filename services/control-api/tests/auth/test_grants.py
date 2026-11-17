"""Generated decision fixtures. These do not prove SQL, IAM, provider or cell isolation."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from opentour_control.auth.grant_models import (
    CAPABILITIES,
    AccessGrant,
    CreateGrant,
    GrantError,
    GrantOperationView,
    GrantVocabulary,
    Principal,
    ResourceScope,
    SupportNoteRead,
    SupportNoteRef,
    SupportNoteWrite,
    TemplateBinding,
    timestamp,
)
from opentour_control.auth.grant_repository import RepositoryScope
from opentour_control.auth.grants import (
    AuthorityState,
    GrantAncestry,
    GrantResolver,
    PrincipalRecord,
    ResourceRecord,
)
from opentour_control.auth.session import AuthContext, VerifiedIdentityContext
from opentour_control.authority.coordinator import (
    ActorSource,
    IntentSource,
    JournalCallerContext,
    PostgresAuthorityResolver,
    PostgresJournalRetryPort,
    TransactionSource,
)
from opentour_control.authority.journal_client import RetryBinding, RetryClaim
from opentour_journal.crypto import canonical, sha256
from opentour_journal.models import AppendIntent, AppendOutcome, ImmutableMetadataRef, JournalError
from opentour_journal.ports import ApprovedRegistration, AuthorizedScope, ServicePrincipal
from pydantic import ValidationError

NOW = datetime(2026, 11, 16, 3, 0, tzinfo=UTC)
ORG = "local-org-a"


def scope(**changes):
    result = dict(
        workspace_ids=[],
        project_ids=[],
        environment_ids=[],
        artifact_refs=[],
        support_note_scopes=[],
    )
    result.update(changes)
    return result


def request(**changes):
    result = dict(
        principal={"type": "user", "id": "local-user-b"},
        capabilities=["training.run"],
        resource_scope=scope(project_ids=["project-a"]),
        purposes=["development"],
        data_classes=["customer_confidential"],
        valid_from=timestamp(NOW),
        expires_at=timestamp(NOW + timedelta(hours=1)),
        reason_code="scoped-work",
        expected_authority_revision="authority-a",
    )
    result.update(changes)
    return result


def grant(grant_id="parent", **changes):
    body = request(principal={"type": "user", "id": "local-user-a"})
    body.pop("expected_authority_revision")
    body.update(
        grant_id=grant_id,
        revision="1",
        state="active",
        parent_grant_ids=[],
        template_bindings=[dict(template_id="generated-policy", revision="1", sha256="a" * 64)],
        creation_operation_id="created-" + grant_id,
        creation_authority_revision="authority-a",
    )
    body["resource_scope"]["organization_id"] = ORG
    body.update(changes)
    return AccessGrant.model_validate(body)


class GeneratedResources:
    def __init__(self):
        self.rows = {
            ("workspace", "workspace-a"): ResourceRecord(
                "workspace", "workspace-a", ORG, "1", "active", None, None, None
            ),
            ("project", "project-a"): ResourceRecord(
                "project", "project-a", ORG, "1", "active", "workspace-a", None, None
            ),
            ("project", "project-b"): ResourceRecord(
                "project", "project-b", ORG, "1", "active", "workspace-a", None, None
            ),
            ("workspace", "workspace-b"): ResourceRecord(
                "workspace", "workspace-b", "local-org-b", "1", "active", None, None, None
            ),
        }

    def current(self, org, kind, resource_id):
        return self.rows.get((kind, resource_id))


class GeneratedPrincipals:
    def __init__(self):
        self.rows = {
            id_: PrincipalRecord(
                Principal(type="user", id=id_),
                ORG,
                "active",
                "human",
                "1",
                "https://identity.example.invalid",
                provider_id,
            )
            for id_, provider_id in (
                ("local-user-a", "provider-subject-a"),
                ("local-user-b", "provider-subject-b"),
            )
        }

    def current(self, org, principal):
        return self.rows.get(principal.id)


@pytest.fixture
def grants():
    resources, principals = GeneratedResources(), GeneratedPrincipals()
    context = AuthContext(
        VerifiedIdentityContext(
            {
                "kind": "human",
                "issuer": "https://identity.example.invalid",
                "subject": "provider-subject-a",
            },
            "session-a",
            2,
            timestamp(NOW),
            timestamp(NOW),
        ),
        ORG,
        "local-membership-a",
        "authority-a",
        7,
    )
    vocabulary = GrantVocabulary(
        frozenset({"development", "review", "administration", "support_case_note"}),
        frozenset({"scoped-work"}),
        lambda _p, _t: False,
    )
    resolver = GrantResolver(resources, principals, vocabulary, clock=lambda: NOW)
    authority = AuthorityState(ORG, "authority-a", 7, "current", None)
    management = grant(
        "manage",
        capabilities=["access.manage"],
        purposes=["administration"],
        data_classes=["control_metadata"],
        resource_scope=ResourceScope(organization_id=ORG, **scope(workspace_ids=["workspace-a"])),
    )
    result = SimpleNamespace(
        resolver=resolver,
        resources=resources,
        principals=principals,
        context=context,
        actor=principals.rows["local-user-a"],
        authority=authority,
        rows={"parent": grant(), "manage": management},
        now=NOW,
    )
    resolver.clock = lambda: result.now
    return result


def decide(
    g,
    *,
    action="training.run",
    project="project-a",
    purpose="development",
    classification="customer_confidential",
):
    return g.resolver.decide(
        g.context,
        g.actor,
        g.authority,
        g.rows,
        action=action,
        resource_kind="project",
        resource_id=project,
        purpose=purpose,
        data_class=classification,
        now=g.now,
    )


def delegate(g, body=None):
    return g.resolver.delegation(
        g.context,
        g.actor,
        g.authority,
        g.rows,
        workspace_id="workspace-a",
        body=CreateGrant.model_validate(request() if body is None else body),
        management_purpose="administration",
        now=g.now,
    )


def test_exact_capability_and_project_positive(grants):
    grants.rows["evidence"] = grant(
        "evidence",
        capabilities=["evidence.read"],
        resource_scope=ResourceScope(organization_id=ORG, **scope(project_ids=["project-b"])),
    )
    assert decide(grants).grant_ids == ("parent",)
    assert decide(grants, action="evidence.read", project="project-b").grant_ids == ("evidence",)


@pytest.mark.parametrize(
    "action,project", [("training.run", "project-b"), ("evidence.read", "project-a")]
)
def test_exact_capability_and_project_adverse(grants, action, project):
    with pytest.raises(GrantError, match="CAPABILITY_DENIED"):
        decide(grants, action=action, project=project)


@pytest.mark.parametrize("field", list(request()))
def test_missing_required_field_rejected(field):
    body = request()
    del body[field]
    with pytest.raises(ValidationError):
        CreateGrant.model_validate(body)


@pytest.mark.parametrize(
    "capabilities", [["admin"], ["*"], ["scope.scan"], [], ["training.run"] * 2]
)
def test_no_grant_on_invalid_authority_adverse(capabilities):
    with pytest.raises(ValidationError):
        CreateGrant.model_validate(request(capabilities=capabilities))


def test_no_grant_on_invalid_authority_positive(grants):
    plan = delegate(grants)
    assert plan.parent_grant_ids == ("parent",)
    assert plan.resource_revisions == (
        ("project", "project-a", "1"),
        ("workspace", "workspace-a", "1"),
    )


@pytest.mark.parametrize("extra", ["organization_id", "role", "is_admin", "authority_revision"])
def test_closed_request_rejects_injected_authority(extra):
    with pytest.raises(ValidationError):
        CreateGrant.model_validate(request(**{extra: ORG}))


def test_derived_organization_not_in_input():
    with pytest.raises(ValidationError):
        CreateGrant.model_validate(request(resource_scope=scope(organization_id=ORG)))


@pytest.mark.parametrize(
    "body", [b"{}", b"null", b"[]", b'{"principal":{},"principal":{}}', b"\xff"]
)
def test_malformed_request_is_safe_error(body):
    with pytest.raises(GrantError, match="SCHEMA_INVALID"):
        CreateGrant.parse(body)


@pytest.mark.parametrize("revision", ["0", "01", "+1", "1\n", " 1", "9223372036854775808", 1, True])
def test_revision_precision_adverse(revision):
    with pytest.raises(ValidationError):
        grant(revision=revision)


def test_revision_precision_positive():
    assert grant(revision="9223372036854775807").revision == "9223372036854775807"


@pytest.mark.parametrize(
    "timestamp_value",
    ["2026-11-16T03:00:00Z\n", "2026-11-16T03:00:00+00:00", "2026-02-30T00:00:00Z"],
)
def test_timestamp_adverse(timestamp_value):
    with pytest.raises(ValidationError):
        CreateGrant.model_validate(request(valid_from=timestamp_value))


def test_validity_boundaries_positive(grants):
    assert decide(grants).authority_revision == "authority-a"
    grants.now = NOW - timedelta(microseconds=1)
    with pytest.raises(GrantError, match="CAPABILITY_DENIED"):
        decide(grants)


def test_validity_boundaries_adverse(grants):
    grants.rows["parent"] = grant(expires_at="2026-11-16T04:00:00Z")
    grants.now = NOW + timedelta(hours=1)
    with pytest.raises(GrantError, match="CAPABILITY_DENIED"):
        decide(grants)


@pytest.mark.parametrize("kind", ["workload", "support"])
def test_workload_support_expiry_required(kind):
    with pytest.raises(ValidationError):
        CreateGrant.model_validate(
            request(principal={"type": kind, "id": "generated"}, expires_at=None)
        )


def test_nullable_human_expiry_adverse(grants):
    with pytest.raises(GrantError, match="EXPIRY_POLICY_REQUIRED"):
        delegate(grants, request(expires_at=None))


def test_nullable_human_expiry_positive(grants):
    binding = TemplateBinding(template_id="generated-durable", revision="2", sha256="b" * 64)
    grants.resolver.vocabulary = replace(
        grants.resolver.vocabulary,
        allow_durable_human_expiry=lambda principal, bindings: (
            principal.type == "user" and bindings == (binding,)
        ),
    )
    grants.rows["parent"] = grant(expires_at=None)
    result = grants.resolver.delegation(
        grants.context,
        grants.actor,
        grants.authority,
        grants.rows,
        workspace_id="workspace-a",
        body=CreateGrant.model_validate(request(expires_at=None)),
        management_purpose="administration",
        now=NOW,
        templates=(binding,),
    )
    assert result.parent_grant_ids == ("parent",)


@pytest.mark.parametrize(
    "changed",
    [
        {"data_classes": ["protected_evaluation"]},
        {"purposes": ["review"]},
        {"resource_scope": scope(project_ids=["project-a", "project-b"])},
        {"capabilities": ["training.run", "deployments.promote"]},
        {"expires_at": timestamp(NOW + timedelta(hours=2))},
    ],
)
def test_delegation_ceiling_adverse(grants, changed):
    with pytest.raises(GrantError, match="DELEGATION_CEILING"):
        delegate(grants, request(**changed))


def test_delegation_no_dimension_cross_product(grants):
    grants.rows["other"] = grant(
        "other", purposes=["review"], data_classes=["protected_evaluation"]
    )
    with pytest.raises(GrantError, match="DELEGATION_CEILING"):
        delegate(grants, request(purposes=["review"], data_classes=["customer_confidential"]))


@pytest.mark.parametrize("state", ["prepared", "revoked"])
def test_prepared_or_revoked_is_not_active(grants, state):
    grants.rows["parent"] = grant(state=state)
    with pytest.raises(GrantError, match="CAPABILITY_DENIED"):
        decide(grants)


@pytest.mark.parametrize(
    "state,operation",
    [("reconciliation_required", None), ("unavailable", None), ("current", "pending")],
)
def test_current_unresolved_authority_denies(grants, state, operation):
    grants.authority = replace(grants.authority, state=state, current_operation_id=operation)
    with pytest.raises(GrantError, match="AUTHORITY_UNAVAILABLE"):
        decide(grants)


def test_stale_revision_never_rebases(grants):
    with pytest.raises(GrantError, match="STALE_AUTHORITY"):
        delegate(grants, request(expected_authority_revision="older"))
    grants.authority = replace(grants.authority, revocation_epoch=8)
    with pytest.raises(GrantError, match="STALE_AUTHORITY"):
        decide(grants)


@pytest.mark.parametrize("role", ["trainer", "human"])
def test_trainer_protected_duty_adverse(grants, role):
    p = Principal(type="workload", id="trainer-generated")
    grants.principals.rows[p.id] = PrincipalRecord(
        p,
        ORG,
        "active",
        role,
        "1",
        "https://workloads.example.invalid",
        "trainer-subject",
    )
    with pytest.raises(GrantError, match="DUTY_CONFLICT|PRINCIPAL_UNAVAILABLE"):
        delegate(grants, request(principal=p, data_classes=["protected_evaluation"]))


def test_trainer_release_duty_conflict(grants):
    p = Principal(type="workload", id="trainer-generated")
    grants.principals.rows[p.id] = PrincipalRecord(
        p, ORG, "active", "trainer", "1", "workload-issuer", "trainer"
    )
    with pytest.raises(GrantError, match="DUTY_CONFLICT"):
        delegate(grants, request(principal=p, capabilities=["deployments.promote"]))


def test_viewer_billing_admin_adverse(grants):
    grants.rows = {
        "viewer": grant("viewer", capabilities=["evidence.read"]),
        "billing": grant("billing", capabilities=["billing.write"]),
    }
    with pytest.raises(GrantError, match="CAPABILITY_DENIED"):
        decide(grants, action="deployments.promote")


def test_unknown_action_mapping_adverse(grants):
    with pytest.raises(GrantError, match="UNKNOWN_ACTION"):
        decide(grants, action="train.all")


def test_explicit_train_alias_positive(grants):
    assert decide(grants, action="train").grant_ids == ("parent",)
    assert "train" not in CAPABILITIES


def test_foreign_resource_is_not_disclosed(grants):
    grants.resources.rows[("project", "project-a")] = replace(
        grants.resources.rows[("project", "project-a")],
        organization_id="local-org-b",
    )
    with pytest.raises(GrantError, match="RESOURCE_NOT_FOUND") as failure:
        decide(grants)
    assert "local-org-b" not in str(failure.value.wire("request"))


def test_foreign_principal_is_not_disclosed(grants):
    grants.principals.rows["local-user-b"] = replace(
        grants.principals.rows["local-user-b"], organization_id="other"
    )
    with pytest.raises(GrantError, match="RESOURCE_NOT_FOUND"):
        delegate(grants)


def test_principal_identity_cannot_be_substituted(grants):
    grants.actor = grants.principals.rows["local-user-b"]
    with pytest.raises(GrantError, match="PRINCIPAL_UNAVAILABLE"):
        decide(grants)


def test_workspaceproject_lifecycle_adverse(grants):
    grants.resources.rows[("workspace", "workspace-a")] = replace(
        grants.resources.rows[("workspace", "workspace-a")],
        state="archived",
    )
    with pytest.raises(GrantError, match="RESOURCE_UNAVAILABLE"):
        decide(grants)


def test_empty_scope_never_wildcard(grants):
    grants.rows["parent"] = grant(resource_scope=ResourceScope(organization_id=ORG, **scope()))
    with pytest.raises(GrantError, match="CAPABILITY_DENIED"):
        decide(grants)


def test_project_scope_cannot_authorize_organization_metadata(grants):
    grants.resources.rows[("organization", ORG)] = ResourceRecord(
        "organization", ORG, ORG, "1", "active", None, None, None
    )
    grants.rows["parent"] = grant(capabilities=["support.create"])
    with pytest.raises(GrantError, match="CAPABILITY_DENIED"):
        grants.resolver.decide(
            grants.context,
            grants.actor,
            grants.authority,
            grants.rows,
            action="support.create",
            resource_kind="organization",
            resource_id=ORG,
            purpose="development",
            data_class="customer_confidential",
            now=NOW,
        )


def test_delegation_cannot_drop_child_ids_to_gain_organization_scope(grants):
    with pytest.raises(GrantError, match="DELEGATION_CEILING"):
        delegate(grants, request(resource_scope=scope()))


def test_management_expiry_during_target_lookup_denies_delegation(grants):
    grants.rows["manage"] = grants.rows["manage"].model_copy(
        update={"expires_at": timestamp(NOW + timedelta(seconds=1))}
    )
    lookup = grants.resources.current

    def delayed(org, kind, resource_id):
        if kind == "project":
            grants.now = NOW + timedelta(seconds=1)
        return lookup(org, kind, resource_id)

    grants.resources.current = delayed
    with pytest.raises(GrantError, match="CAPABILITY_DENIED"):
        delegate(grants)
    assert decide(grants).grant_ids == ("parent",)


def test_delegation_retains_only_management_support_current_after_lookup(grants):
    original = grants.rows["manage"]
    grants.rows["manage"] = original.model_copy(
        update={"expires_at": timestamp(NOW + timedelta(seconds=1))}
    )
    grants.rows["manage-backup"] = original.model_copy(update={"grant_id": "manage-backup"})
    lookup = grants.resources.current

    def delayed(org, kind, resource_id):
        if kind == "project":
            grants.now = NOW + timedelta(seconds=1)
        return lookup(org, kind, resource_id)

    grants.resources.current = delayed
    plan = delegate(grants)
    assert plan.parent_grant_ids == ("parent",)
    assert plan.management_grant_ids == ("manage-backup",)
    assert plan.observed_at == grants.now


def test_delegation_denies_clock_rollback_after_management_lookup(grants):
    lookup = grants.resources.current

    def time_changes(org, kind, resource_id):
        if kind == "workspace" and grants.now == NOW:
            grants.now = NOW + timedelta(seconds=2)
        elif kind == "project":
            grants.now = NOW + timedelta(seconds=1)
        return lookup(org, kind, resource_id)

    grants.resources.current = time_changes
    with pytest.raises(GrantError, match="CLOCK_UNAVAILABLE"):
        delegate(grants)


def test_final_management_fence_rejects_missing_retained_support(grants):
    plan = delegate(grants)
    grants.rows["another-manager"] = grants.rows.pop("manage").model_copy(
        update={"grant_id": "another-manager"}
    )
    with pytest.raises(GrantError, match="CAPABILITY_DENIED"):
        grants.resolver.require_management_current(
            grants.rows, plan.management_grant_ids, organization_id=ORG, now=NOW
        )


def test_ancestry_missing_or_revoked_adverse(grants):
    grants.rows["parent"] = grant(parent_grant_ids=["root"])
    with pytest.raises(GrantError, match="CAPABILITY_DENIED"):
        decide(grants)
    grants.rows["root"] = grant("root", state="revoked")
    with pytest.raises(GrantError, match="CAPABILITY_DENIED"):
        decide(grants)


def test_ancestry_current_positive_then_ceiling_change(grants):
    grants.rows["parent"] = grant(parent_grant_ids=["root"])
    grants.rows["root"] = grant("root")
    assert decide(grants).grant_ids == ("parent", "root")
    grants.rows["root"] = grant("root", capabilities=["evidence.read"], revision="2")
    with pytest.raises(GrantError, match="CAPABILITY_DENIED"):
        decide(grants)


def test_ancestry_cycle_denied(grants):
    grants.rows["parent"] = grant(parent_grant_ids=["root"])
    grants.rows["root"] = grant("root", parent_grant_ids=["parent"])
    with pytest.raises(GrantError, match="CAPABILITY_DENIED"):
        decide(grants)


def test_scheduled_grant_checks_parents_without_authorizing_current_use():
    parent = grant("root")
    child = grant(
        "scheduled", parent_grant_ids=["root"], valid_from=timestamp(NOW + timedelta(seconds=1))
    )
    ancestry = GrantAncestry({"scheduled": child, "root": parent}, NOW, ORG)
    assert child.temporal_state(NOW) == "not_yet_valid"
    assert not ancestry.valid(child)
    assert ancestry.parents_valid(child)


@pytest.mark.parametrize(
    "fault",
    ["missing", "revoked", "prepared", "future", "foreign", "cycle", "weakened", "mismatched-id"],
)
def test_scheduled_ancestry_fails_closed(fault):
    child = grant(
        "scheduled", parent_grant_ids=["root"], valid_from=timestamp(NOW + timedelta(seconds=2))
    )
    root = grant("root")
    if fault in {"revoked", "prepared"}:
        root = root.model_copy(update={"state": fault})
    elif fault == "future":
        root = root.model_copy(update={"valid_from": timestamp(NOW + timedelta(seconds=1))})
    elif fault == "foreign":
        root = root.model_copy(
            update={
                "resource_scope": root.resource_scope.model_copy(
                    update={"organization_id": "local-org-b"}
                )
            }
        )
    elif fault == "cycle":
        root = root.model_copy(update={"parent_grant_ids": ("scheduled",)})
    elif fault == "weakened":
        root = root.model_copy(update={"capabilities": ("evidence.read",)})
    elif fault == "mismatched-id":
        root = root.model_copy(update={"grant_id": "different-root"})
    rows = {"scheduled": child}
    if fault != "missing":
        rows["root"] = root
    assert not GrantAncestry(rows, NOW, ORG).parents_valid(child)


@pytest.fixture
def scoped_support_notes(grants):
    write = SupportNoteWrite(
        resource_type="support_note_write",
        case_id="case-a",
        environment_id="environment-a",
        write_intent_id="note-intent-a",
        max_utf8_bytes=16384,
    )
    read = SupportNoteRead(
        resource_type="support_note_read",
        case_id="case-a",
        note_ref=SupportNoteRef(environment_id="environment-a", note_id="note-a", revision="1"),
    )
    records = grants.resources.rows
    records[("environment", "environment-a")] = ResourceRecord(
        "environment", "environment-a", ORG, "1", "active", "workspace-a", "project-a", None
    )
    for resource_id, note_scope in (("note-intent-a", write), ("note-a", read)):
        records[("note", resource_id)] = ResourceRecord(
            "note",
            resource_id,
            ORG,
            "1",
            "active",
            "workspace-a",
            "project-a",
            "environment-a",
            note_scope=note_scope,
        )
    grants.rows["support-notes"] = grant(
        "support-notes",
        capabilities=["support.note.create", "support.note.read", "evidence.read"],
        resource_scope=ResourceScope(
            organization_id=ORG,
            **scope(workspace_ids=["workspace-a"], support_note_scopes=[write, read]),
        ),
        purposes=["support_case_note", "development"],
        data_classes=["customer_confidential", "control_metadata"],
    )
    return grants


@pytest.mark.parametrize(
    "action,resource_id",
    [("support.note.create", "note-intent-a"), ("support.note.read", "note-a")],
)
def test_note_capability_binds_typed_operation(scoped_support_notes, action, resource_id):
    g = scoped_support_notes
    result = g.resolver.decide(
        g.context,
        g.actor,
        g.authority,
        g.rows,
        action=action,
        resource_kind="note",
        resource_id=resource_id,
        purpose="support_case_note",
        data_class="customer_confidential",
        now=NOW,
    )
    assert result.grant_ids == ("support-notes",)


@pytest.mark.parametrize(
    "action,kind,resource_id,purpose,data_class",
    [
        (
            "support.note.read",
            "note",
            "note-intent-a",
            "support_case_note",
            "customer_confidential",
        ),
        ("support.note.create", "note", "note-a", "support_case_note", "customer_confidential"),
        (
            "support.note.read",
            "workspace",
            "workspace-a",
            "support_case_note",
            "customer_confidential",
        ),
        ("evidence.read", "note", "note-a", "support_case_note", "customer_confidential"),
        ("support.note.read", "note", "note-a", "development", "customer_confidential"),
        ("support.note.read", "note", "note-a", "support_case_note", "control_metadata"),
    ],
)
def test_note_capability_cannot_substitute_operation_scope_purpose_or_class(
    scoped_support_notes, action, kind, resource_id, purpose, data_class
):
    g = scoped_support_notes
    with pytest.raises(GrantError, match="CAPABILITY_DENIED"):
        g.resolver.decide(
            g.context,
            g.actor,
            g.authority,
            g.rows,
            action=action,
            resource_kind=kind,
            resource_id=resource_id,
            purpose=purpose,
            data_class=data_class,
            now=NOW,
        )


@pytest.mark.parametrize("fault", ["wrong-environment", "wrong-note-id"])
def test_typed_note_coordinates_must_match_canonical_resource(scoped_support_notes, fault):
    g = scoped_support_notes
    resource = g.resources.rows[("note", "note-a")]
    if fault == "wrong-environment":
        resource = replace(resource, environment_id="environment-b")
    else:
        resource = replace(
            resource,
            note_scope=SupportNoteRead(
                resource_type="support_note_read",
                case_id="case-a",
                note_ref=SupportNoteRef(
                    environment_id="environment-a", note_id="different-note-id", revision="1"
                ),
            ),
        )
    g.resources.rows[("note", "note-a")] = resource
    with pytest.raises(GrantError, match="RESOURCE_NOT_FOUND"):
        g.resolver.resource(ORG, "note", "note-a")


def operation(**changes):
    value = dict(
        operation_id="op",
        operation_kind="access_grant_create",
        operation_revision="1",
        grant_id="grant",
        state="prepared",
        creation_receipt=None,
        current_grant_status=None,
        current_status_unavailable_reason=None,
        request_id="request",
    )
    value.update(changes)
    return value


def creation():
    return dict(
        grant_id="grant",
        revision="1",
        authority_revision="creation-authority",
        revocation_epoch=7,
        committed_at=timestamp(NOW),
    )


@pytest.mark.parametrize("state", ["prepared", "reconciliation_required", "aborted"])
def test_non_success_cannot_include_active_receipt(state):
    with pytest.raises(ValidationError):
        GrantOperationView.model_validate(operation(state=state, creation_receipt=creation()))


def test_historical_receipt_and_current_revoked_are_distinct():
    current = dict(
        grant_id="grant",
        revision="2",
        state="revoked",
        authority_status="current",
        observed_at=timestamp(NOW),
    )
    view = GrantOperationView.model_validate(
        operation(state="succeeded", creation_receipt=creation(), current_grant_status=current)
    )
    assert view.creation_receipt.revision == "1"
    assert view.current_grant_status.revision == "2"


def test_succeeded_missing_current_status_needs_explicit_reason():
    with pytest.raises(ValidationError):
        GrantOperationView.model_validate(operation(state="succeeded", creation_receipt=creation()))
    view = GrantOperationView.model_validate(
        operation(
            state="succeeded",
            creation_receipt=creation(),
            current_status_unavailable_reason="AUTHORITY_UNAVAILABLE",
        )
    )
    assert view.current_grant_status is None


def test_receipt_grant_mismatch_is_invalid():
    receipt = creation()
    receipt["grant_id"] = "other"
    with pytest.raises(ValidationError):
        GrantOperationView.model_validate(
            operation(
                state="succeeded",
                creation_receipt=receipt,
                current_status_unavailable_reason="AUTHORITY_UNAVAILABLE",
            )
        )


def test_resource_lookup_crossing_expiry_denied(grants):
    original = grants.resources.current

    def delayed(org, kind, id_):
        grants.now = NOW + timedelta(hours=1)
        return original(org, kind, id_)

    grants.resources.current = delayed
    with pytest.raises(GrantError, match="CAPABILITY_DENIED"):
        decide(grants)


def test_clock_rollback_during_lookup_denied(grants):
    original = grants.resources.current

    def delayed(org, kind, id_):
        grants.now = NOW - timedelta(seconds=1)
        return original(org, kind, id_)

    grants.resources.current = delayed
    with pytest.raises(GrantError, match="CLOCK_UNAVAILABLE"):
        decide(grants)


@pytest.mark.parametrize(
    "offset,state", [(-1, "not_yet_valid"), (0, "active"), (3599, "active"), (3600, "expired")]
)
def test_future_validity_projection_exact_boundaries(offset, state):
    value = grant(valid_from="2026-11-16T03:00:00Z", expires_at="2026-11-16T04:00:00.000Z")
    assert value.temporal_state(NOW + timedelta(seconds=offset)) == state
    assert value.revision == "1"


def test_future_validity_never_overrides_known_revocation():
    value = grant(state="revoked", valid_from=timestamp(NOW + timedelta(minutes=10)))
    assert value.temporal_state(NOW) == "revoked"


def test_future_timer_cannot_activate_prepared_grant():
    value = grant(state="prepared")
    with pytest.raises(GrantError, match="AUTHORITY_UNAVAILABLE"):
        value.temporal_state(NOW + timedelta(seconds=1))


def test_four_customer_approval_capabilities_are_registered_but_not_automatic(grants):
    for capability in [
        "policy.authorize",
        "source.authorize",
        "model_rights.authorize",
        "budget.authorize",
    ]:
        assert capability in CAPABILITIES
        with pytest.raises(GrantError, match="CAPABILITY_DENIED"):
            decide(grants, action=capability, classification="control_metadata")


@pytest.fixture
def journal_sources():
    """Generated SQL-result fixtures test binding only; actual storage tests are separate."""
    rows = {}
    context = {"session_id": "session-a", "session_generation": 2, "membership_id": "membership-a"}
    operation = dict(
        operation_kind="access_grant_create",
        organization_id=ORG,
        operation_id="operation-a",
        grant_id="grant-a",
        issuer="https://identity.example.invalid",
        subject="provider-subject-a",
        request_body=request(),
        proposed_grant=grant().wire(),
        plan={"parents": ["parent"]},
        proposed_epoch=7,
        proposed_authority_revision="authority-b",
        context=context,
        state="prepared",
        outcome_body=None,
    )

    def record(kind, value):
        raw = canonical(value.wire())
        ref = ImmutableMetadataRef(record_id=kind + "-a", revision="1", sha256=sha256(raw))
        rows[ref.record_id] = dict(
            operation_id="operation-a", kind=kind, revision="1", sha256=ref.sha256, body=raw
        )
        return ref

    actor = ActorSource(
        schema_version="opentour.grant-actor-source.v1",
        organization_id=ORG,
        operation_id="operation-a",
        issuer=operation["issuer"],
        subject=operation["subject"],
        **context,
    )
    actor_ref = record("actor", actor)
    source = IntentSource(
        schema_version="opentour.grant-intent-source.v1",
        organization_id=ORG,
        operation_id="operation-a",
        grant_id="grant-a",
        request_sha256=sha256(canonical(operation["request_body"])),
        proposed_grant_sha256=sha256(canonical(operation["proposed_grant"])),
        plan_sha256=sha256(canonical(operation["plan"])),
        actor_ref=actor_ref,
        mutation_kind="grant",
        proposed_epoch=7,
        proposed_authority_revision="authority-b",
        expected_head_sequence=0,
        expected_head_digest="a" * 64,
    )
    intent = AppendIntent(
        organization_id=ORG,
        operation_id="operation-a",
        expected_head_sequence=0,
        expected_head_digest="a" * 64,
        mutation_kind="grant",
        proposed_epoch=7,
        proposed_authority_revision="authority-b",
        canonical_intent_digest=source.digest(),
        scoped_intent_ref=record("intent", source),
        actor_ref=actor_ref,
        created_at=timestamp(NOW),
    )
    operation["intent_body"] = canonical(intent.wire())
    transaction = TransactionSource(
        schema_version="opentour.grant-transaction-source.v1",
        organization_id=ORG,
        operation_id="operation-a",
        grant_id="grant-a",
        intent_content_sha256=sha256(operation["intent_body"]),
        proposed_grant_sha256=source.proposed_grant_sha256,
        result="committed",
        resulting_epoch=7,
        resulting_authority_revision="authority-b",
    )
    outcome = AppendOutcome(
        organization_id=ORG,
        operation_id="operation-a",
        expected_head_sequence=1,
        expected_head_digest="b" * 64,
        intent_sequence=1,
        result="committed",
        control_transaction_ref=record("transaction", transaction),
        resulting_epoch=7,
        resulting_authority_revision="authority-b",
    )
    operation["outcome_body"] = canonical(outcome.wire())
    fixture = SimpleNamespace(
        operation=operation,
        rows=rows,
        intent=intent,
        outcome=outcome,
        grant={"creation_operation_id": "operation-a", "body": operation["proposed_grant"]},
        entered=0,
    )

    class GeneratedRepository:
        role = "coordinator"

        @contextmanager
        def transaction(self, scope):
            assert scope.organization_id == ORG
            fixture.entered += 1
            yield self

        def retained_operation(self, conn, organization_id, operation_id):
            if (organization_id, operation_id) != (ORG, "operation-a"):
                raise GrantError("RESOURCE_NOT_FOUND", 404)
            return operation

        def execute(self, sql, parameters):
            assert parameters[0] == ORG
            if "opentour_authority.sources" in sql:
                row = rows.get(parameters[1])
            elif "opentour_authority.grants" in sql:
                row = fixture.grant
            else:
                raise AssertionError("Unexpected SQL in generated unit fixture")
            return SimpleNamespace(fetchone=lambda: row)

    principal = ServicePrincipal(
        "generated-audit-service",
        ImmutableMetadataRef(record_id="service-registration-a", revision="1", sha256="c" * 64),
    )
    caller = SimpleNamespace(
        current=lambda p, org, cap: JournalCallerContext(
            p,
            AuthorizedScope(p.service_id, org, cap),
            RepositoryScope(org, "generated-service-issuer", p.service_id, "service-request-a"),
        )
    )
    enrollment = ApprovedRegistration(ORG, 7, "authority-a", principal.authentication_ref)
    enrollments = SimpleNamespace(current=lambda _p, _org: enrollment)
    fixture.principal, fixture.callers, fixture.enrollments = principal, caller, enrollments
    fixture.resolver = PostgresAuthorityResolver(GeneratedRepository(), caller, enrollments)
    return fixture


def test_journal_source_binds_exact_canonical_intent_and_sql_commit(journal_sources):
    g = journal_sources
    resolved = g.resolver.resolve_intent(g.principal, g.intent)
    assert resolved.canonical_intent_digest == g.intent.canonical_intent_digest
    result = g.resolver.reconcile_transaction(g.principal, g.outcome, g.intent)
    assert result.result == "committed" and result.resulting_authority_revision == "authority-b"
    assert result.intent_content_sha256 == sha256(canonical(g.intent.wire()))


@pytest.mark.parametrize("field", ["request_body", "proposed_grant", "plan"])
def test_journal_source_changed_reservation_is_not_authority(journal_sources, field):
    g = journal_sources
    g.operation[field] = {"changed": True}
    with pytest.raises(JournalError, match="JOURNAL_RECONCILIATION_REQUIRED"):
        g.resolver.resolve_intent(g.principal, g.intent)


@pytest.mark.parametrize("field", ["operation_id", "kind", "revision", "sha256", "body"])
def test_journal_source_ref_or_bytes_substitution_denied(journal_sources, field):
    g = journal_sources
    row = g.rows[g.intent.scoped_intent_ref.record_id]
    row[field] = b'{"other":true}' if field == "body" else "other"
    with pytest.raises(JournalError):
        g.resolver.resolve_intent(g.principal, g.intent)


def test_journal_source_noncanonical_identical_json_denied(journal_sources):
    g = journal_sources
    row = g.rows[g.intent.scoped_intent_ref.record_id]
    row["body"] += b" "
    with pytest.raises(JournalError):
        g.resolver.resolve_intent(g.principal, g.intent)


@pytest.mark.parametrize(
    "field,value",
    [
        ("issuer", "foreign-issuer"),
        ("subject", "foreign-subject"),
        (
            "context",
            {"session_id": "foreign", "session_generation": 2, "membership_id": "membership-a"},
        ),
    ],
)
def test_journal_source_actor_context_substitution_denied(journal_sources, field, value):
    g = journal_sources
    g.operation[field] = value
    with pytest.raises(JournalError, match="JOURNAL_RECONCILIATION_REQUIRED"):
        g.resolver.resolve_intent(g.principal, g.intent)


@pytest.mark.parametrize(
    "mutation", ["missing_source", "unknown_outcome", "missing_grant", "wrong_grant"]
)
def test_journal_missing_or_unknown_sql_never_proves_commit(journal_sources, mutation):
    g = journal_sources
    if mutation == "missing_source":
        del g.rows[g.outcome.control_transaction_ref.record_id]
    elif mutation == "unknown_outcome":
        g.operation["outcome_body"] = None
    elif mutation == "missing_grant":
        g.grant = None
    else:
        g.grant = {"creation_operation_id": "foreign", "body": g.operation["proposed_grant"]}
    with pytest.raises(JournalError, match="JOURNAL_RECONCILIATION_REQUIRED"):
        g.resolver.reconcile_transaction(g.principal, g.outcome, g.intent)


@pytest.mark.parametrize("operation_state", ["prepared", "reconciliation_required", "aborted"])
def test_known_sql_abort_does_not_require_premature_public_abort(journal_sources, operation_state):
    g = journal_sources
    g.operation["state"] = operation_state
    g.grant = None
    source = TransactionSource(
        schema_version="opentour.grant-transaction-source.v1",
        organization_id=ORG,
        operation_id="operation-a",
        grant_id="grant-a",
        intent_content_sha256=sha256(canonical(g.intent.wire())),
        proposed_grant_sha256=sha256(canonical(g.operation["proposed_grant"])),
        result="aborted",
        resulting_epoch=7,
        resulting_authority_revision="authority-a",
    )
    raw = canonical(source.wire())
    reference = ImmutableMetadataRef(record_id="transaction-a", revision="1", sha256=sha256(raw))
    g.rows[reference.record_id] = dict(
        operation_id="operation-a",
        kind="transaction",
        revision="1",
        sha256=reference.sha256,
        body=raw,
    )
    outcome = g.outcome.model_copy(
        update={
            "result": "aborted",
            "control_transaction_ref": reference,
            "resulting_authority_revision": "authority-a",
        }
    )
    g.operation["outcome_body"] = canonical(outcome.wire())
    resolved = g.resolver.reconcile_transaction(g.principal, outcome, g.intent)
    assert resolved.result == "aborted"
    assert g.operation["state"] == operation_state  # Resolver never advances public state.
    g.operation["state"] = "succeeded"
    with pytest.raises(JournalError, match="JOURNAL_RECONCILIATION_REQUIRED"):
        g.resolver.reconcile_transaction(g.principal, outcome, g.intent)
    g.operation["state"] = operation_state
    del g.rows[reference.record_id]
    with pytest.raises(JournalError, match="JOURNAL_RECONCILIATION_REQUIRED"):
        g.resolver.reconcile_transaction(g.principal, outcome, g.intent)


def test_journal_changed_expected_head_is_not_rebased(journal_sources):
    g = journal_sources
    changed = g.intent.model_copy(update={"expected_head_sequence": 2})
    with pytest.raises(JournalError, match="JOURNAL_OPERATION_CONFLICT"):
        g.resolver.resolve_intent(g.principal, changed)


@pytest.mark.parametrize("substitution", ["principal", "organization", "capability"])
def test_journal_caller_authority_binding_precedes_sql(journal_sources, substitution):
    g = journal_sources
    original = g.callers.current

    def changed(p, org, cap):
        value = original(p, org, cap)
        if substitution == "principal":
            return replace(value, principal=replace(p, service_id="foreign"))
        if substitution == "organization":
            return replace(value, sql_scope=replace(value.sql_scope, organization_id="foreign"))
        return replace(value, authorization=AuthorizedScope(p.service_id, org, "read"))

    g.callers.current = changed
    with pytest.raises(JournalError, match="JOURNAL_FORBIDDEN"):
        g.resolver.resolve_intent(g.principal, g.intent)
    assert g.entered == 0


def test_journal_service_fault_is_safe_and_has_no_sdk_cause(journal_sources):
    g = journal_sources

    def unavailable(*_):
        raise RuntimeError("generated-private-diagnostic")

    g.callers.current = unavailable
    with pytest.raises(JournalError, match="JOURNAL_FORBIDDEN") as raised:
        g.resolver.resolve_intent(g.principal, g.intent)
    assert raised.value.__suppress_context__ and raised.value.__cause__ is None
    assert g.entered == 0


def test_journal_enrollment_needs_current_exact_registered_tenant(journal_sources):
    g = journal_sources
    assert g.resolver.registration(g.principal, ORG).organization_id == ORG
    g.enrollments.current = lambda p, org: ApprovedRegistration(
        "foreign", 0, "other", p.authentication_ref
    )
    with pytest.raises(JournalError, match="JOURNAL_FORBIDDEN"):
        g.resolver.registration(g.principal, ORG)


def test_durable_retry_diagnostics_cannot_store_arbitrary_provider_text(journal_sources):
    g = journal_sources
    port = PostgresJournalRetryPort(
        g.resolver.repository, RepositoryScope(ORG, "issuer", "subject", "session"), None
    )
    changes = []
    port._change = lambda *_args, **kwargs: changes.append(kwargs)
    claim = RetryClaim(
        RetryBinding(ORG, "operation-a", "intent", sha256(canonical(g.intent.wire()))),
        canonical(g.intent.wire()),
        "token",
        "1",
        1,
        NOW,
        NOW,
    )
    port.hold(claim, "generated-private-diagnostic")
    assert changes == [{"code": "JOURNAL_PROTOCOL_REJECTED"}]
    with pytest.raises(JournalError, match="JOURNAL_RETRY_STATE_INVALID"):
        port.schedule(claim, NOW + timedelta(seconds=1), "generated-private-diagnostic")
