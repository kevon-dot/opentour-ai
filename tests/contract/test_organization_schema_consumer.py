"""Offline frozen organization schema integration, not tenant authority evidence."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from tools import schema_validate

URI = "urn:opentour:organization:1.0"


def valid(definition: str, value: Any) -> bool:
    return schema_validate.valid_instance(
        f"{URI}#/$defs/{definition}", value, schema_validate.schema_registry()
    )


def test_canonical_create_receipt_is_closed_and_not_workspace_readiness() -> None:
    receipt = {
        "organization_id": "generated-org",
        "operation_id": "generated-operation",
        "state": "identity_provisioning",
        "revision": "1",
        "request_id": "generated-request",
    }
    assert valid("CreateReceipt", receipt)
    assert not valid("CreateReceipt", {**receipt, "state": "active"})
    assert not valid("CreateReceipt", {**receipt, "workspace_ready": True})


@pytest.mark.parametrize("revision", ["1", "9007199254740993", "9223372036854775807"])
def test_revision_reference_preserves_exact_canonical_decimal(revision: str) -> None:
    assert valid("RevisionRequest", {"expected_revision": revision})


@pytest.mark.parametrize("revision", [None, True, 1, "01", "0", "1\n", "9223372036854775808"])
def test_revision_coercion_whitespace_and_overflow_refused(revision: Any) -> None:
    assert not valid("RevisionRequest", {"expected_revision": revision})


@pytest.mark.parametrize(
    "effect", ["not_dispatched", "known_no_effect", "known_retained", "outcome_unknown"]
)
def test_cancelled_projection_preserves_separate_remote_effect_state(effect: str) -> None:
    value = {
        "organization_id": "generated-org",
        "operation_id": "generated-operation",
        "state": "cancelled",
        "revision": "2",
        "request_id": "generated-request",
        "retry_available": False,
        "cancel_available": False,
        "reason_code": "SETUP_CANCELLED",
        "updated_at": "2026-11-16T00:00:00.000Z",
        "remote_effect_state": effect,
    }
    assert valid("OperationView", value)
    assert not valid("OperationView", {**value, "remote_effect_state": "provider_deleted"})
    assert not valid("OperationView", {**value, "provider_credentials": "generated-sentinel"})


def test_error_resolves_canonical_common_shape() -> None:
    value = {
        "code": "SCHEMA_INVALID",
        "message": "Generated safe refusal",
        "request_id": "generated",
        "retryable": False,
        "field_errors": [],
        "current_revision": None,
    }
    assert valid("Error", value)
    assert not valid("Error", {**value, "current_revision": 1})


def test_changed_owned_schema_fails_hash_binding(tmp_path: Path) -> None:
    shutil.copytree(schema_validate.ROOT / "packages/contracts", tmp_path / "packages/contracts")
    path = tmp_path / "packages/contracts/platform/organization.schema.json"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(schema_validate.SchemaError, match="hash mismatch"):
        schema_validate.schema_registry(tmp_path)
