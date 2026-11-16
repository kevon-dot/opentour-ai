"""Closed grant wire shapes; no schema instance establishes grant authority."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from opentour_control.auth.grant_models import CreateGrant, GrantOperationView
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
    from opentour_control.auth.grant_models import CAPABILITIES

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
