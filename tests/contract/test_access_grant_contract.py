"""Closed grant wire shapes; no schema instance establishes grant authority."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from opentour_control.auth.grant_models import (
    APPROVED_CREATOR_ENTITLEMENTS,
    APPROVED_CREATOR_POLICY_SHA256,
    CAPABILITIES,
    AccessGrant,
    CreateGrant,
    GrantError,
    GrantOperationView,
    InitialAuthorityPolicy,
    InputResourceScope,
    ResourceScope,
)
from pydantic import ValidationError
from referencing import Resource

from tools.schema_validate import schema_registry, valid_instance

ROOT = Path(__file__).resolve().parents[2]
URI = "urn:opentour:access-grant:1.0"
SCHEMA = json.loads((ROOT / "packages/contracts/platform/access-grant.schema.json").read_text())
REGISTRY = schema_registry().with_resource(URI, Resource.from_contents(SCHEMA))
BODY: dict[str, Any] = {
    "principal": {"type": "user", "id": "generated-recipient"},
    "capabilities": ["training.run"],
    "resource_scope": {
        "workspace_ids": [],
        "project_ids": ["generated-project"],
        "environment_ids": [],
        "artifact_refs": [],
        "support_note_scopes": [],
    },
    "purposes": ["development"],
    "data_classes": ["customer_confidential"],
    "valid_from": "2026-11-16T03:00:00Z",
    "expires_at": "2026-11-16T04:00:00Z",
    "reason_code": "scoped-work",
    "expected_authority_revision": "opaque-authority",
}


def valid(name: str, value: Any) -> bool:
    return valid_instance(URI + "#/$defs/" + name, value, REGISTRY)


def test_closed_schema_and_common_refs_resolve_offline() -> None:
    Draft202012Validator.check_schema(SCHEMA)
    assert valid("CreateGrant", BODY)
    assert CreateGrant.model_validate(BODY).resource_scope.project_ids == ("generated-project",)
    for definition in SCHEMA["$defs"].values():
        assert definition["type"] == "object"
        assert definition["additionalProperties"] is False


@pytest.mark.parametrize("field", list(BODY))
def test_each_create_field_required(field: str) -> None:
    changed = copy.deepcopy(BODY)
    del changed[field]
    assert not valid("CreateGrant", changed)


@pytest.mark.parametrize("extra", ["organization_id", "parent_grant_ids", "state", "admin"])
def test_input_cannot_name_trusted_authority(extra: str) -> None:
    assert not valid("CreateGrant", {**BODY, extra: "generated"})


def test_no_organization_field_in_input_scope() -> None:
    changed = copy.deepcopy(BODY)
    changed["resource_scope"]["organization_id"] = "other"
    assert not valid("CreateGrant", changed)


@pytest.mark.parametrize("count", [0, 51])
def test_capability_count_bounds(count: int) -> None:
    changed = copy.deepcopy(BODY)
    changed["capabilities"] = sorted(CAPABILITIES)[:count]
    assert not valid("CreateGrant", changed)


@pytest.mark.parametrize("field", ["capabilities", "purposes", "data_classes"])
def test_unique_dimensions(field: str) -> None:
    changed = copy.deepcopy(BODY)
    changed[field] *= 2
    assert not valid("CreateGrant", changed)


@pytest.mark.parametrize("scope_count", [0, 1, 100, 101])
def test_support_note_scope_finite_count(scope_count: int) -> None:
    changed = copy.deepcopy(BODY)
    changed["resource_scope"]["support_note_scopes"] = [
        {
            "resource_type": "support_note_write",
            "case_id": "generated-case",
            "environment_id": "generated-environment",
            "write_intent_id": f"intent-{i}",
            "max_utf8_bytes": 16384,
        }
        for i in range(scope_count)
    ]
    assert valid("CreateGrant", changed) is (scope_count <= 100)
    if scope_count <= 100:
        assert (
            len(CreateGrant.model_validate(changed).resource_scope.support_note_scopes)
            == scope_count
        )
    else:
        with pytest.raises(ValidationError):
            CreateGrant.model_validate(changed)


@pytest.mark.parametrize(
    "revision", ["1", "9223372036854775807", "01", "1\n", "9223372036854775808", 1]
)
def test_note_scope_uses_exact_common_revision(revision: Any) -> None:
    changed = copy.deepcopy(BODY)
    changed["resource_scope"]["support_note_scopes"] = [
        {
            "resource_type": "support_note_read",
            "case_id": "generated-case",
            "note_ref": {
                "environment_id": "generated-environment",
                "note_id": "generated-note",
                "revision": revision,
            },
        }
    ]
    expected = revision in {"1", "9223372036854775807"}
    assert valid("CreateGrant", changed) is expected
    if expected:
        CreateGrant.model_validate(changed)
    else:
        with pytest.raises(ValidationError):
            CreateGrant.model_validate(changed)


def test_note_scope_unknown_field_and_duplicates_denied() -> None:
    changed = copy.deepcopy(BODY)
    note = {
        "resource_type": "support_note_write",
        "case_id": "case",
        "environment_id": "env",
        "write_intent_id": "intent",
        "max_utf8_bytes": 16384,
    }
    changed["resource_scope"]["support_note_scopes"] = [note, note]
    assert not valid("CreateGrant", changed)
    changed["resource_scope"]["support_note_scopes"] = [{**note, "content": "not admitted"}]
    assert not valid("CreateGrant", changed)


@pytest.mark.parametrize("kind", ["user", "workload", "support"])
def test_only_human_wire_can_carry_null_expiry(kind: str) -> None:
    changed = copy.deepcopy(BODY)
    changed["principal"]["type"] = kind
    changed["expires_at"] = None
    assert valid("CreateGrant", changed) is (kind == "user")
    # Human shape still requires an approved validity policy at admission.


def view(state: str) -> dict[str, Any]:
    return {
        "operation_id": "op",
        "operation_kind": "access_grant_create",
        "operation_revision": "1",
        "grant_id": "grant",
        "state": state,
        "creation_receipt": None,
        "current_grant_status": None,
        "current_status_unavailable_reason": None,
        "request_id": "req",
    }


def receipt() -> dict[str, Any]:
    return {
        "grant_id": "grant",
        "revision": "1",
        "authority_revision": "opaque",
        "revocation_epoch": 0,
        "committed_at": "2026-11-16T03:00:00Z",
    }


@pytest.mark.parametrize("state", ["prepared", "reconciliation_required", "aborted"])
def test_pending_and_abort_never_carry_active_receipt(state: str) -> None:
    value = view(state)
    assert valid("GrantOperationView", value)
    value["creation_receipt"] = receipt()
    assert not valid("GrantOperationView", value)


def test_historical_success_can_report_current_authority_unavailable() -> None:
    value = view("succeeded")
    value["creation_receipt"] = receipt()
    assert not valid("GrantOperationView", value)
    value["current_status_unavailable_reason"] = "AUTHORITY_UNAVAILABLE"
    assert valid("GrantOperationView", value)
    assert GrantOperationView.model_validate(value).current_grant_status is None


@pytest.mark.parametrize("state", ["active", "expired", "revoked"])
def test_creation_and_current_state_are_separate(state: str) -> None:
    value = view("succeeded")
    value["creation_receipt"] = receipt()
    value["current_grant_status"] = {
        "grant_id": "grant",
        "revision": "2",
        "state": state,
        "authority_status": "current",
        "observed_at": "2026-11-16T05:00:00Z",
    }
    assert valid("GrantOperationView", value)
    parsed = GrantOperationView.model_validate(value)
    assert parsed.creation_receipt is not None and parsed.creation_receipt.revision == "1"


@pytest.mark.parametrize("revision", ["01", "0", "1\n", 1, "9223372036854775808"])
def test_receipt_revision_does_not_coerce(revision: Any) -> None:
    value = receipt()
    value["revision"] = revision
    assert not valid("GrantCreationReceipt", value)


def test_receipt_semantics_reject_mismatched_grant_ids() -> None:
    value = view("succeeded")
    value["creation_receipt"] = receipt()
    value["creation_receipt"]["grant_id"] = "foreign"
    value["current_status_unavailable_reason"] = "AUTHORITY_UNAVAILABLE"
    with pytest.raises(ValidationError):
        GrantOperationView.model_validate(value)


ARRAY_BOUNDARIES = [
    (definition, field, maximum)
    for definition, fields in [
        (
            "InputResourceScope",
            [
                (name, 100)
                for name in ("workspace_ids", "project_ids", "environment_ids", "artifact_refs")
            ],
        ),
        (
            "ResourceScope",
            [
                (name, 100)
                for name in ("workspace_ids", "project_ids", "environment_ids", "artifact_refs")
            ],
        ),
        ("CreateGrant", [("purposes", 100), ("data_classes", 4)]),
        (
            "AccessGrant",
            [
                ("purposes", 100),
                ("data_classes", 4),
                ("parent_grant_ids", 100),
                ("template_bindings", 50),
            ],
        ),
    ]
    for field, maximum in fields
]


@pytest.mark.parametrize("definition,field,maximum", ARRAY_BOUNDARIES)
@pytest.mark.parametrize("overflow", [False, True])
def test_schema_and_model_array_bounds_match(
    definition: str,
    field: str,
    maximum: int,
    overflow: bool,
) -> None:
    count = maximum + int(overflow)
    if definition in {"InputResourceScope", "ResourceScope"}:
        value = copy.deepcopy(BODY["resource_scope"])
        if definition == "ResourceScope":
            value["organization_id"] = "generated-org"
    else:
        value = copy.deepcopy(BODY)
        if definition == "AccessGrant":
            del value["expected_authority_revision"]
            value["resource_scope"]["organization_id"] = "generated-org"
            value.update(
                grant_id="generated-grant",
                revision="1",
                state="active",
                parent_grant_ids=[],
                template_bindings=[],
                creation_operation_id="generated-operation",
                creation_authority_revision="generated-authority",
            )
    if field == "artifact_refs":
        value[field] = [
            {
                "artifact_id": f"generated-artifact-{index}",
                "kind": "generated",
                "revision": "1",
                "sha256": "a" * 64,
                "manifest_id": "generated-manifest",
                "environment_id": "generated-env",
                "classification": "control_metadata",
                "purpose_grant_id": "generated-purpose",
                "expires_at": None,
            }
            for index in range(count)
        ]
    elif field == "template_bindings":
        value[field] = [
            {"template_id": f"generated-template-{index}", "revision": "1", "sha256": "a" * 64}
            for index in range(count)
        ]
    elif field == "data_classes":
        value[field] = [
            "control_metadata",
            "customer_confidential",
            "protected_evaluation",
            "model_artifact",
        ] + (["control_metadata"] if overflow else [])
    else:
        value[field] = [f"generated-{index}" for index in range(count)]
    shape = SCHEMA["$defs"][definition]["properties"][field]
    assert shape["maxItems"] == maximum and "maxLength" not in shape
    assert valid(definition, value) is not overflow
    model = {
        "InputResourceScope": InputResourceScope,
        "ResourceScope": ResourceScope,
        "CreateGrant": CreateGrant,
        "AccessGrant": AccessGrant,
    }[definition]
    if overflow:
        with pytest.raises(ValidationError):
            model.model_validate(value)
    else:
        model.model_validate(value)


BIRTH_TUPLES = {
    "owner-administration-v1": (
        "organization_administration",
        ("members.manage", "access.manage", "members.invite", "workspace.create"),
    ),
    "owner-commercial-v1": (
        "billing_administration",
        ("billing.read", "billing.write", "quotas.manage"),
    ),
    "owner-customer-support-v1": (
        "customer_support",
        ("support.create", "support.read", "support.manage", "support.close", "support.delegate"),
    ),
    "owner-customer-approval-v1": (
        "customer_policy_approval",
        ("policy.authorize", "source.authorize", "model_rights.authorize", "budget.authorize"),
    ),
}


@pytest.fixture
def creator_policy() -> dict[str, Any]:
    return {
        "policy_ref": {
            "record_id": "generated-policy",
            "revision": "1",
            "sha256": APPROVED_CREATOR_POLICY_SHA256,
        },
        "template_bindings": [
            {"template_id": template, "revision": "1", "sha256": "ab" * 32}
            for template in BIRTH_TUPLES
        ],
        "authenticated_installer": {
            "record_id": "generated-installer",
            "revision": "1",
            "sha256": "cd" * 32,
        },
        "enrolled_organization_id": "generated-org",
        "enrollment_ref": {
            "record_id": "generated-enrollment",
            "revision": "1",
            "sha256": "ef" * 32,
        },
    }


def test_explicit_creator_entitlement_matches_approved_materialization_matrix() -> None:
    rows = []
    for group in APPROVED_CREATOR_ENTITLEMENTS:
        materialization = asdict(group)
        capabilities = materialization.pop("capabilities")
        rows.extend({"capability": capability, **materialization} for capability in capabilities)
    names = {row["capability"] for row in rows}
    assert len(rows) == len(names) == 94
    assert CAPABILITIES - names == {
        "evaluations.inspect_protected",
        "benchmarks.disclose",
        "incident.severity_review",
        "operations.read",
        "operations.execute",
        "support.emergency_request",
        "support.emergency_approve",
        "security.admin",
    }
    normalized = json.dumps(
        sorted(rows, key=lambda row: row["capability"]), sort_keys=True, separators=(",", ":")
    ).encode()
    assert hashlib.sha256(normalized).hexdigest() == (
        "800d21229592a6a4c6b6a39df1f717f6daf798a46e5d10d81f3d6f97b6aeb9cc"
    )


def test_creator_birth_grants_are_prepared_single_capability_tuples(
    creator_policy: dict[str, Any],
) -> None:
    policy = InitialAuthorityPolicy.model_validate(creator_policy)
    now = datetime(2026, 11, 16, 5, 1, 0, 123456, tzinfo=UTC)
    birth = policy.birth_grants("generated-org", "local-creator", "original-install", "rev-a", now)
    assert len(birth) == len({grant.grant_id for grant in birth}) == 16
    assert birth == policy.birth_grants(
        "generated-org", "local-creator", "original-install", "rev-a", now
    )
    observed = set()
    for grant in birth:
        assert grant.principal.wire() == {"type": "user", "id": "local-creator"}
        assert grant.resource_scope.organization_id == "generated-org"
        assert grant.resource_scope.organization_only()
        assert grant.data_classes == ("control_metadata",)
        assert grant.valid_from == "2026-11-16T05:01:00.123456Z" and grant.expires_at is None
        assert (
            grant.state,
            grant.revision,
            grant.creation_operation_id,
            grant.creation_authority_revision,
        ) == ("prepared", "1", "original-install", "rev-a")
        assert not grant.parent_grant_ids and not grant.current(now)
        assert valid("AccessGrant", grant.wire())
        (capability,) = grant.capabilities
        (template,) = grant.template_bindings
        (purpose,) = grant.purposes
        observed.add((template.template_id, purpose, capability))
    assert observed == {
        (template, purpose, c)
        for template, (purpose, capabilities) in BIRTH_TUPLES.items()
        for c in capabilities
    }


@pytest.mark.parametrize("invalid_policy", ["wrong_digest", "excess", "duplicate", "missing"])
def test_creator_policy_refuses_unapproved_configuration(
    creator_policy: dict[str, Any], invalid_policy: str
) -> None:
    bindings = creator_policy["template_bindings"]
    if invalid_policy == "wrong_digest":
        creator_policy["policy_ref"]["sha256"] = "00" * 32
    elif invalid_policy == "excess":
        bindings.append({**bindings[0], "template_id": "unapproved-template"})
    elif invalid_policy == "duplicate":
        bindings.append(bindings[0].copy())
    else:
        bindings.pop()
    with pytest.raises(ValidationError, match="Unapproved creator policy binding"):
        InitialAuthorityPolicy.model_validate(creator_policy)


def test_creator_birth_refuses_foreign_enrollment_and_binds_operation_identity(
    creator_policy: dict[str, Any],
) -> None:
    policy = InitialAuthorityPolicy.model_validate(creator_policy)
    now = datetime(2026, 11, 16, 5, 1, tzinfo=UTC)
    with pytest.raises(GrantError, match="CREATOR_POLICY_UNAVAILABLE"):
        policy.birth_grants("different-org", "local-creator", "install-a", "rev-a", now)
    birth = policy.birth_grants("generated-org", "local-creator", "install-a", "rev-a", now)
    renamed = policy.birth_grants("generated-org", "local-creator", "install-b", "rev-a", now)
    changed_principal = policy.birth_grants(
        "generated-org", "other-person", "install-a", "rev-a", now
    )
    other_enrollment = InitialAuthorityPolicy.model_validate(
        {
            **creator_policy,
            "enrollment_ref": {**creator_policy["enrollment_ref"], "record_id": "other-enrollment"},
        }
    )
    reenrolled = other_enrollment.birth_grants(
        "generated-org", "local-creator", "install-a", "rev-a", now
    )
    original_ids = {grant.grant_id for grant in birth}
    assert all(
        original_ids.isdisjoint(grant.grant_id for grant in other)
        for other in (renamed, changed_principal, reenrolled)
    )
