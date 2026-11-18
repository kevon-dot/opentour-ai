"""Provider-neutral checks for the OPE-181 closed installer input shape."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from referencing import Registry, Resource

from tools.schema_validate import read_json, schema_registry, valid_instance

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ID = "urn:opentour:cell-installer-parameters:1.0"
PARAMETER_NAMES = (
    "organization_id",
    "environment_id",
    "cell_id",
    "mode",
    "account_id",
    "region",
    "control_endpoint",
    "controller_digest",
    "evaluator_digest",
    "trainer_digest",
    "kms_policy_profile",
    "network_profile",
    "subnet_ids",
    "security_group_ids",
    "gateway_hostname",
    "certificate_arn",
    "retention_policy_ref",
)


@pytest.fixture(scope="module")
def registry() -> Registry[Any]:
    document = read_json(ROOT / "infrastructure/cloudformation/parameters.schema.json")
    assert document["$id"] == SCHEMA_ID
    return schema_registry(ROOT).with_resource(SCHEMA_ID, Resource.from_contents(document))


@pytest.fixture
def parameters() -> dict[str, Any]:
    return {
        "organization_id": "fixture-org",
        "environment_id": "fixture-environment",
        "cell_id": "fixture-cell",
        "mode": "opentour_managed",
        "account_id": "000000000001",
        "region": "us-east-1",
        "control_endpoint": "fixture-control",
        "controller_digest": "a" * 64,
        "evaluator_digest": "b" * 64,
        "trainer_digest": "c" * 64,
        "kms_policy_profile": "fixture-kms",
        "network_profile": "fixture-managed-network",
        "subnet_ids": ["subnet-00000000000000001", "subnet-00000002"],
        "security_group_ids": ["sg-00000000000000001"],
        "gateway_hostname": "fixture-gateway.example",
        "certificate_arn": "arn:aws:acm:us-east-1:000000000001:certificate/"
        "00000000-0000-0000-0000-000000000001",
        "retention_policy_ref": {"policy_id": "fixture-retention", "revision": "fixture-revision"},
    }


@pytest.mark.parametrize("mode", ["opentour_managed", "customer_controlled"])
def test_common_parameter_shape_in_both_modes(
    registry: Registry[Any], parameters: dict[str, Any], mode: str
) -> None:
    parameters["mode"] = mode
    parameters["network_profile"] = f"fixture-{mode}-network"
    assert set(parameters) == set(PARAMETER_NAMES)
    assert valid_instance(SCHEMA_ID, parameters, registry)


def test_gateway_keys_can_explicitly_be_null(
    registry: Registry[Any], parameters: dict[str, Any]
) -> None:
    parameters.update(gateway_hostname=None, certificate_arn=None)
    assert valid_instance(SCHEMA_ID, parameters, registry)


def test_commercial_region_syntax_does_not_hard_code_the_fixture_region(
    registry: Registry[Any], parameters: dict[str, Any]
) -> None:
    parameters.update(region="ap-southeast-7", gateway_hostname=None, certificate_arn=None)
    assert valid_instance(SCHEMA_ID, parameters, registry)


@pytest.mark.parametrize("name", PARAMETER_NAMES)
def test_all_parameter_keys_are_required(
    registry: Registry[Any], parameters: dict[str, Any], name: str
) -> None:
    del parameters[name]
    assert not valid_instance(SCHEMA_ID, parameters, registry)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("user_data", "#!/bin/sh"),
        ("code_url", "https://fixture.example/installer.sh"),
        ("credentials", {"token": "fixture-only"}),
        ("access_key", "fixture-only"),
        ("extra", True),
    ],
)
def test_additional_parameters_are_rejected(
    registry: Registry[Any], parameters: dict[str, Any], name: str, value: Any
) -> None:
    parameters[name] = value
    assert not valid_instance(SCHEMA_ID, parameters, registry)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("organization_id", ""),
        ("mode", "managed"),
        ("mode", None),
        ("account_id", 123456789012),
        ("account_id", "12345678901"),
        ("account_id", "123456789012\n"),
        ("region", "us-east"),
        ("region", "us-gov-west-1"),
        ("region", "cn-north-1"),
        ("control_endpoint", "https://fixture.example/control"),
        ("control_endpoint", "fixture-control\n"),
        ("kms_policy_profile", {"policy": "inline"}),
        ("network_profile", "echo fixture"),
        ("controller_digest", "sha256:" + "a" * 64),
        ("evaluator_digest", "A" * 64),
        ("trainer_digest", "c" * 63),
        ("trainer_digest", "c" * 64 + "\n"),
        ("subnet_ids", []),
        ("subnet_ids", ["subnet-00000001", "subnet-00000001"]),
        ("subnet_ids", ["sg-00000001"]),
        ("subnet_ids", ["subnet-00000001\n"]),
        ("security_group_ids", []),
        ("security_group_ids", ["sg-00000001", "sg-00000001"]),
        ("security_group_ids", ["subnet-00000001"]),
        ("gateway_hostname", "https://fixture-gateway.example"),
        ("gateway_hostname", "fixture..example"),
        ("gateway_hostname", "*.fixture.example"),
        ("certificate_arn", "arn:aws:iam::000000000001:role/fixture"),
        ("certificate_arn", ""),
        ("retention_policy_ref", "fixture-retention"),
        ("retention_policy_ref", {"policy_id": "fixture-retention"}),
        ("retention_policy_ref", {"policy_id": "fixture", "revision": "fixture", "seconds": 3600}),
    ],
)
def test_invalid_parameter_values_are_rejected(
    registry: Registry[Any], parameters: dict[str, Any], name: str, value: Any
) -> None:
    parameters[name] = value
    assert not valid_instance(SCHEMA_ID, parameters, registry)
