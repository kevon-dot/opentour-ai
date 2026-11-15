"""Generated report/compatibility fixtures, not an actual OPE-299 collector."""

from __future__ import annotations

import copy
import hashlib
import shutil

import pytest

from tools import schema_validate
from tools.validate_spec import (
    ROOT,
    TARGET,
    ConfigurationError,
    compare_action_binding,
    cycle_path,
    validate_manifest,
    validate_ui_report,
)


def report():
    return {
        "manifest_digest": "a" * 64,
        "provider_schema_digest": "b" * 64,
        "figma_revision": "approved-v4",
        "checked_commit": "c" * 40,
        "expected_actions": ["UI-SENTINEL-A01", "UI-SENTINEL-A02"],
        "checked_actions": ["UI-SENTINEL-A01", "UI-SENTINEL-A02"],
        "failures": [],
        "status": "PASS",
    }


def check_report(value):
    return validate_ui_report(
        value,
        expected_actions={"UI-SENTINEL-A01", "UI-SENTINEL-A02"},
        manifest_digest="a" * 64,
        provider_schema_digest="b" * 64,
        figma_revision="approved-v4",
        commit="c" * 40,
    )


def test_generated_report_conforms_without_runtime_acceptance():
    assert check_report(report()) == []


@pytest.mark.parametrize("field", ["expected_actions", "checked_actions"])
def test_exact_action_set_cannot_be_replaced_by_count_or_duplicate(field):
    value = report()
    value[field] = 2
    with pytest.raises(ConfigurationError):
        check_report(value)
    value[field] = ["UI-SENTINEL-A01", "UI-SENTINEL-A01"]
    with pytest.raises(ConfigurationError):
        check_report(value)
    value[field] = ["UI-SENTINEL-A01", "UI-ORPHAN-A01"]
    assert "ACTION_SET_MISMATCH" in check_report(value)


@pytest.mark.parametrize(
    "field", ["manifest_digest", "provider_schema_digest", "figma_revision", "checked_commit"]
)
def test_stale_source_or_design_or_tested_commit_fails(field):
    value = report()
    value[field] = "stale"
    assert f"STALE_{field.upper()}" in check_report(value)


def test_lost_action_assertion_or_explicit_failure_is_nonpassing():
    value = report()
    value["checked_actions"].pop()
    value["failures"] = [
        {
            "action_id": "UI-SENTINEL-A02",
            "owner": "OPE-299",
            "reason": "Required denied assertion was not collected",
        }
    ]
    assert set(check_report(value)) == {"ACTION_SET_MISMATCH", "ACTION_ASSERTIONS_FAILED"}


def operation():
    return {
        "method": "POST",
        "path_template": "/v1/invitations:accept",
        "plane": "control",
        "capability": "invitations.accept",
        "request_fields": ["token"],
        "request_schema_ref": "urn:sentinel:invitation-request:1",
        "response_schema_refs": {"200": "urn:sentinel:invitation-response:1"},
    }


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("method", "GET"),
        ("path_template", "/v1/invitations/{id}:accept"),
        ("request_fields", ["invitation_token"]),
        ("plane", "hosted-bff"),
        ("capability", "members.admin"),
        ("request_schema_ref", "urn:sentinel:wrong:1"),
        ("response_schema_refs", {"200": "urn:sentinel:wrong:1"}),
    ],
)
def test_provider_route_body_plane_and_schema_drift_fail(field, replacement):
    binding, provider = operation(), operation()
    binding[field] = replacement
    assert compare_action_binding(
        binding,
        provider,
        parameter_mapping={},
        required_assertions={"denied"},
        observed_assertions={"denied"},
    )


def test_explicit_bijective_placeholder_mapping_preserves_literal_path():
    binding, provider = operation(), operation()
    binding["path_template"] = "/v1/resources/{resource_id}"
    provider["path_template"] = "/v1/resources/{id}"
    kwargs = {
        "required_assertions": {"positive", "denied", "stale"},
        "observed_assertions": {"positive", "denied", "stale"},
    }
    assert not compare_action_binding(
        binding, provider, parameter_mapping={"resource_id": "id"}, **kwargs
    )
    assert compare_action_binding(binding, provider, parameter_mapping={}, **kwargs)
    binding["path_template"] = "/v1/other/{resource_id}"
    assert compare_action_binding(
        binding, provider, parameter_mapping={"resource_id": "id"}, **kwargs
    )


def test_lost_required_adverse_assertion_is_not_hidden_by_other_passes():
    assert compare_action_binding(
        operation(),
        operation(),
        parameter_mapping={},
        required_assertions={"positive", "denied", "unknown"},
        observed_assertions={"positive", "denied"},
    ) == ["MISSING_REQUIRED_ADVERSE_ASSERTION"]


def test_parent_containment_is_not_a_dependency_edge():
    assert cycle_path({"parent": [], "child": ["parent"]}) == []
    assert cycle_path({"a": ["b"], "b": ["c"], "c": ["a"]}) == ["a", "b", "c", "a"]


def test_actual_diagram_asset_hash_and_missing_asset(tmp_path):
    value = schema_validate.read_json(ROOT / TARGET)
    asset = tmp_path / "diagram.mmd"
    asset.write_text("graph TD\n A-->B\n")
    shutil.copytree(ROOT / "packages/contracts/common", tmp_path / "packages/contracts/common")
    (tmp_path / "tests/contract").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "tests/contract/test_traceability.py",
        tmp_path / "tests/contract/test_traceability.py",
    )
    entry = {
        "path": "diagram.mmd",
        "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
        "schema_id": None,
        "owner": "OPE-126",
    }
    shutil.copytree(ROOT / "docs/specification/assets", tmp_path / "docs/specification/assets")
    value["diagram_assets"].append(entry)
    assert not [c for c in validate_manifest(value, tmp_path) if c.status == "FAIL"]
    asset.write_text("graph TD\n A-->C\n")
    assert any(
        c.code == "LOCAL_ASSET_SOURCE_HASH" and c.status == "FAIL"
        for c in validate_manifest(value, tmp_path)
    )
    asset.unlink()
    assert any(
        c.code == "LOCAL_ASSET_SOURCE_HASH" and c.status == "FAIL"
        for c in validate_manifest(value, tmp_path)
    )


def test_closed_inputs_refuse_unknown_fields():
    value = report()
    value["trusted_signature"] = True
    with pytest.raises(ConfigurationError):
        check_report(value)
    value = schema_validate.read_json(ROOT / TARGET)
    value["mappings"][0]["assume_pass"] = True
    with pytest.raises(ConfigurationError):
        validate_manifest(copy.deepcopy(value))
