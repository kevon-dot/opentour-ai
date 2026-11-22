"""Generated fixtures for the cell command context and its new-command preflight."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from opentour_cell.runner.protocol import (
    CellCommandScope,
    CommandContextError,
    parse_cell_command_context,
    require_current_command_context,
)

NOW = datetime(2026, 11, 21, 13, 0, 10, tzinfo=UTC)
EXPIRES = NOW + timedelta(seconds=10)


@pytest.fixture
def command():
    return {
        "schema_version": "1.0",
        "request_id": "generated-request",
        "request_nonce": "generated-nonce",
        "organization_id": "generated-organization",
        "workspace_id": "generated-workspace",
        "project_id": "generated-project",
        "environment_id": "generated-environment",
        "cell_id": "generated-cell",
        "deployment_mode": "opentour_managed",
        "actor": {
            "kind": "human",
            "subject": "generated-actor",
            "issuer": "https://example.com",
        },
        "authority_revision": "generated-authority",
        "revocation_epoch": 2,
        "idempotency_key": "generated-operation",
        "created_at": "2026-11-21T13:00:00.000Z",
        "expires_at": "2026-11-21T13:00:20.000Z",
        "manifest_refs": [
            {
                "artifact_id": "generated-artifact",
                "kind": "input_manifest",
                "revision": "generated-revision",
                "sha256": "a" * 64,
                "manifest_id": "generated-manifest",
                "environment_id": "generated-environment",
                "classification": "customer_confidential",
                "purpose_grant_id": "generated-grant",
            }
        ],
        "expected_revision": "9223372036854775807",
    }


def scope(mode="opentour_managed"):
    return CellCommandScope(
        organization_id="generated-organization",
        workspace_id="generated-workspace",
        project_id="generated-project",
        environment_id="generated-environment",
        cell_id="generated-cell",
        deployment_mode=mode,
        authority_revision="generated-authority",
        revocation_epoch=2,
    )


def parse(command):
    return parse_cell_command_context(json.dumps(command).encode())


@pytest.mark.parametrize("mode", ["opentour_managed", "customer_controlled"])
def test_current_context_is_admitted_in_each_deployment_mode(command, mode):
    command["deployment_mode"] = mode
    parsed = parse(command)
    require_current_command_context(parsed, scope(mode), now=NOW, uncertainty=timedelta(seconds=5))
    assert parsed.model_dump(mode="json", exclude_unset=True) == command


@pytest.mark.parametrize(
    ("delegated_grant_id", "expires_at"),
    [(None, None), ("generated-delegation", "2026-11-21T13:00:25.000Z")],
)
def test_explicit_nested_optional_values_survive_parsing(command, delegated_grant_id, expires_at):
    command["actor"]["delegated_grant_id"] = delegated_grant_id
    command["manifest_refs"][0]["expires_at"] = expires_at
    command["expected_revision"] = None
    assert parse(command).model_dump(mode="json", exclude_unset=True) == command


@pytest.mark.parametrize(
    ("field", "foreign"),
    [
        ("organization_id", "generated-other-organization"),
        ("workspace_id", "generated-other-workspace"),
        ("project_id", "generated-other-project"),
        ("environment_id", "generated-other-environment"),
        ("cell_id", "generated-other-cell"),
        ("deployment_mode", "customer_controlled"),
        ("authority_revision", "generated-other-authority"),
        ("revocation_epoch", 1),
        ("revocation_epoch", 3),
    ],
)
def test_new_command_rejects_scope_or_authority_mismatch(command, field, foreign):
    parsed = parse(command | {field: foreign})
    with pytest.raises(CommandContextError, match="^COMMAND_CONTEXT_MISMATCH$"):
        require_current_command_context(parsed, scope(), now=NOW, uncertainty=timedelta())


def test_trusted_epoch_cannot_be_a_boolean(command):
    parsed = parse(command | {"revocation_epoch": 1})
    with pytest.raises(CommandContextError, match="^COMMAND_CONTEXT_MISMATCH$"):
        require_current_command_context(
            parsed, replace(scope(), revocation_epoch=True), now=NOW, uncertainty=timedelta()
        )


@pytest.mark.parametrize(
    ("now", "uncertainty"),
    [
        (NOW, timedelta(seconds=5, microseconds=1)),
        (NOW, timedelta(microseconds=-1)),
        (NOW.replace(tzinfo=None), timedelta()),
    ],
    ids=["over-five-seconds", "negative-uncertainty", "naive-clock"],
)
def test_new_command_fails_closed_with_untrusted_time(command, now, uncertainty):
    with pytest.raises(CommandContextError, match="^CLOCK_SKEW$"):
        require_current_command_context(parse(command), scope(), now=now, uncertainty=uncertainty)


def test_new_command_refuses_at_conservative_expiry(command):
    parsed = parse(command)
    uncertainty = timedelta(seconds=2)
    require_current_command_context(
        parsed,
        scope(),
        now=EXPIRES - uncertainty - timedelta(microseconds=1),
        uncertainty=uncertainty,
    )
    with pytest.raises(CommandContextError, match="^COMMAND_CONTEXT_EXPIRED$"):
        require_current_command_context(
            parsed, scope(), now=EXPIRES - uncertainty, uncertainty=uncertainty
        )
    with pytest.raises(CommandContextError, match="^COMMAND_CONTEXT_EXPIRED$"):
        require_current_command_context(parsed, scope(), now=EXPIRES, uncertainty=timedelta())


@pytest.mark.parametrize(
    "change",
    [
        {"expected_revision": 1},
        {"expected_revision": "01"},
        {"expected_revision": "9223372036854775808"},
        {"revocation_epoch": True},
        {"revocation_epoch": 2.0},
        {"revocation_epoch": 9_007_199_254_740_992},
        {"schema_version": "1.1"},
        {"created_at": "2026-11-21T13:00:00+00:00"},
        {"created_at": "2026-02-30T13:00:00.000Z"},
        {"expires_at": "2026-11-21T13:00:00.000Z"},
        {"expires_at": "2026-11-21T12:59:59.999Z"},
        {"command_id": "generated-internal-alias"},
    ],
)
def test_rejects_invalid_types_bounds_and_internal_alias(command, change):
    with pytest.raises(CommandContextError, match="^COMMAND_CONTEXT_INVALID$") as failure:
        parse(command | change)
    assert failure.value.__context__ is None


@pytest.mark.parametrize("field", ["workspace_id", "deployment_mode", "expected_revision"])
def test_rejects_missing_required_wire_fields(command, field):
    del command[field]
    with pytest.raises(CommandContextError, match="^COMMAND_CONTEXT_INVALID$"):
        parse(command)


@pytest.mark.parametrize(
    ("original", "malformed"),
    [
        (b'"request_id": ', b'"request_id": "generated-duplicate", "request_id": '),
        (b'"subject": ', b'"subject": "generated-duplicate", "subject": '),
        (b'"sha256": ', b'"sha256": "generated-duplicate", "sha256": '),
        (b'"kind": "human"', b'"kind": "human", "unexpected": "generated-extra"'),
        (b'"revocation_epoch": 2', b'"revocation_epoch": NaN'),
    ],
    ids=["root-duplicate", "actor-duplicate", "ref-duplicate", "nested-extra", "non-json-number"],
)
def test_rejects_ambiguous_or_nonstandard_json(command, original, malformed):
    body = json.dumps(command).encode()
    assert body.count(original) == 1
    with pytest.raises(CommandContextError, match="^COMMAND_CONTEXT_INVALID$"):
        parse_cell_command_context(body.replace(original, malformed))


@pytest.mark.parametrize("body", [b'{"request_id":"\xff"}', b'{"request_id":"\\q"}', b"[]"])
def test_rejects_malformed_or_nonobject_documents(body):
    with pytest.raises(CommandContextError, match="^COMMAND_CONTEXT_INVALID$"):
        parse_cell_command_context(body)
