"""Compare the cell parser with the registered shared command context."""

import json

import pytest
from opentour_cell.runner.protocol import CommandContextError, parse_cell_command_context

from tools.schema_validate import schema_registry, valid_instance

CELL_COMMAND = "urn:opentour:common-contract:1.0#/$defs/CellCommandContext"
CREATED = "2026-01-01T00:00:00.123Z"
EXPIRES = "2026-01-01T00:00:30Z"


@pytest.fixture(scope="module")
def registry():
    return schema_registry()


@pytest.fixture
def command():
    return {
        "schema_version": "1.0",
        "request_id": "generated-request",
        "request_nonce": "generated-request-nonce",
        "idempotency_key": "generated-intent",
        "organization_id": "generated-organization",
        "workspace_id": "generated-workspace",
        "project_id": "generated-project",
        "environment_id": "generated-environment",
        "cell_id": "generated-cell",
        "deployment_mode": "opentour_managed",
        "actor": {"kind": "workload", "subject": "generated-worker", "issuer": "generated-issuer"},
        "authority_revision": "generated-authority-revision",
        "revocation_epoch": 9_007_199_254_740_991,
        "created_at": CREATED,
        "expires_at": EXPIRES,
        "expected_revision": "9223372036854775807",
        "manifest_refs": [
            {
                "artifact_id": "generated-artifact",
                "kind": "checkpoint",
                "revision": "generated-artifact-revision",
                "sha256": "a" * 64,
                "manifest_id": "generated-manifest",
                "environment_id": "generated-environment",
                "classification": "model_artifact",
                "purpose_grant_id": "generated-grant",
            }
        ],
    }


@pytest.mark.parametrize("deployment_mode", ["opentour_managed", "customer_controlled"])
@pytest.mark.parametrize("explicit_nulls", [False, True])
def test_cell_command_context_preserves_schema_approved_wire(
    command, registry, deployment_mode, explicit_nulls
):
    command["deployment_mode"] = deployment_mode
    if explicit_nulls:
        command["expected_revision"] = None
        command["actor"]["delegated_grant_id"] = None
        command["manifest_refs"][0]["expires_at"] = None
    assert valid_instance(CELL_COMMAND, command, registry)
    result = parse_cell_command_context(json.dumps(command).encode("utf-8"))
    assert result.model_dump(mode="json", exclude_unset=True) == command


@pytest.mark.parametrize(
    "required",
    [
        "workspace_id",
        "project_id",
        "environment_id",
        "cell_id",
        "deployment_mode",
        "expected_revision",
    ],
)
def test_parser_and_cell_schema_require_exact_scope(command, registry, required):
    del command[required]
    assert not valid_instance(CELL_COMMAND, command, registry)
    with pytest.raises(CommandContextError, match="^COMMAND_CONTEXT_INVALID$"):
        parse_cell_command_context(json.dumps(command).encode("utf-8"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command_id", "generated-internal-id"),
        ("expected_revision", "9223372036854775808"),
        (
            "actor",
            {"kind": "workload", "subject": "generated-worker", "issuer": "generated", "extra": 1},
        ),
        ("manifest_refs", [{"artifact_id": "missing-bound-metadata"}]),
        ("created_at", "2026-01-01T00:00:00+00:00"),
        ("deployment_mode", "generated-unknown-mode"),
    ],
)
def test_parser_rejects_context_outside_registered_schema(command, registry, field, value):
    command[field] = value
    assert not valid_instance(CELL_COMMAND, command, registry)
    with pytest.raises(CommandContextError, match="^COMMAND_CONTEXT_INVALID$"):
        parse_cell_command_context(json.dumps(command).encode("utf-8"))
