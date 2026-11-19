"""Provider-neutral checks for the OPE-181 closed installer input shape."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from referencing import Registry, Resource

from infrastructure.cell_installer import (
    ApprovedCellProfiles,
    ApprovedCellStorage,
    CellInstallRefused,
    QualifiedProfile,
    RetainedStorageKeyBinding,
    VerifiedCellRelease,
    WorkloadStorageBinding,
    compile_cell_storage,
    prepare_cell_install,
    retained_storage_template_section,
)
from infrastructure.environment_contract import ResourceBinding
from tools.schema_validate import read_json, schema_registry, valid_instance

ROOT = Path(__file__).resolve().parents[2]
CELL_TEMPLATE = ROOT / "infrastructure/cloudformation/cell.yaml"
SCHEMA_ID = "urn:opentour:cell-installer-parameters:1.0"
ENVIRONMENT_PROFILE_ID = "urn:opentour:environment-profile:1.0"
IDENTITY_PARAMETER_NAMES = {
    "environment_id": "environment_id",
    "organization_id": "organization_id",
    "mode": "mode",
    "aws_account_id": "account_id",
    "region": "region",
    "cell_id": "cell_id",
}
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
    resource: Resource[Any] = Resource.from_contents(document)
    return schema_registry(ROOT).with_resources([(SCHEMA_ID, resource)])


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


@pytest.fixture(scope="module")
def profile_registry(registry: Registry[Any]) -> Registry[Any]:
    path = ROOT / "packages/contracts/platform/environment-profile.schema.json"
    document = read_json(path)
    assert document["$id"] == ENVIRONMENT_PROFILE_ID
    resource: Resource[Any] = Resource.from_contents(document)
    return registry.with_resources([(ENVIRONMENT_PROFILE_ID, resource)])


@pytest.fixture
def environment_identity(parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        profile_field: parameters[parameter]
        for profile_field, parameter in IDENTITY_PARAMETER_NAMES.items()
    }


def test_environment_identity_uses_the_same_constraints_as_installer_parameters(
    profile_registry: Registry[Any],
) -> None:
    profile = profile_registry.contents(ENVIRONMENT_PROFILE_ID)["$defs"]["EnvironmentIdentity"]
    parameters = profile_registry.contents(SCHEMA_ID)
    assert set(profile["required"]) == set(IDENTITY_PARAMETER_NAMES)
    assert profile["properties"] == {
        profile_field: parameters["properties"][parameter]
        for profile_field, parameter in IDENTITY_PARAMETER_NAMES.items()
    }


@pytest.mark.parametrize("mode", ["opentour_managed", "customer_controlled"])
def test_environment_identity_from_each_installer_mode(
    profile_registry: Registry[Any],
    environment_identity: dict[str, Any],
    parameters: dict[str, Any],
    mode: str,
) -> None:
    parameters.update(mode=mode, network_profile=f"fixture-{mode}-network")
    environment_identity["mode"] = mode
    assert valid_instance(SCHEMA_ID, parameters, profile_registry)
    assert valid_instance(ENVIRONMENT_PROFILE_ID, environment_identity, profile_registry)


@pytest.mark.parametrize("name", IDENTITY_PARAMETER_NAMES)
def test_all_environment_identity_fields_are_required(
    profile_registry: Registry[Any], environment_identity: dict[str, Any], name: str
) -> None:
    del environment_identity[name]
    assert not valid_instance(ENVIRONMENT_PROFILE_ID, environment_identity, profile_registry)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("organization_id", None),
        ("environment_id", ""),
        ("cell_id", ""),
        ("mode", "managed"),
        ("aws_account_id", 1),
        ("aws_account_id", "000000000001\n"),
        ("region", "us-gov-west-1"),
        ("region", "cn-north-1"),
        ("account_id", "000000000001"),
        ("network_profile", "fixture-network"),
        ("authority_snapshot_ref", "fixture-assertion"),
    ],
)
def test_environment_identity_rejects_invalid_or_foreign_fields(
    profile_registry: Registry[Any], environment_identity: dict[str, Any], name: str, value: Any
) -> None:
    environment_identity[name] = value
    assert not valid_instance(ENVIRONMENT_PROFILE_ID, environment_identity, profile_registry)


@pytest.fixture
def approved_profiles(parameters: dict[str, Any]) -> ApprovedCellProfiles:
    return ApprovedCellProfiles(
        control_endpoint=QualifiedProfile(parameters["control_endpoint"], "d" * 64),
        kms_policy=QualifiedProfile(parameters["kms_policy_profile"], "e" * 64),
        network=QualifiedProfile(parameters["network_profile"], "f" * 64),
        subnet_ids=tuple(parameters["subnet_ids"]),
        security_group_ids=tuple(parameters["security_group_ids"]),
        gateway_hostname=parameters["gateway_hostname"],
        certificate_arn=parameters["certificate_arn"],
        retention_policy_id=parameters["retention_policy_ref"]["policy_id"],
        retention_policy_revision=parameters["retention_policy_ref"]["revision"],
    )


@pytest.fixture
def approved_release(parameters: dict[str, Any]) -> VerifiedCellRelease:
    host = f"{parameters['account_id']}.dkr.ecr.{parameters['region']}.amazonaws.com"
    parameter_schema = ROOT / "infrastructure/cloudformation/parameters.schema.json"
    return VerifiedCellRelease(
        release_sha256="0" * 64,
        template_sha256="1" * 64,
        parameter_schema_sha256=hashlib.sha256(parameter_schema.read_bytes()).hexdigest(),
        images={
            workload: f"{host}/fixture/{workload}@sha256:{parameters[f'{workload}_digest']}"
            for workload in ("controller", "evaluator", "trainer")
        },
    )


@pytest.fixture
def inventory(parameters: dict[str, Any]) -> dict[str, ResourceBinding]:
    scope = f"{parameters['region']}:{parameters['account_id']}"
    references = {
        **{f"arn:aws:ec2:{scope}:subnet/{name}": "subnet" for name in parameters["subnet_ids"]},
        **{
            f"arn:aws:ec2:{scope}:security-group/{name}": "network_policy"
            for name in parameters["security_group_ids"]
        },
        **{
            f"arn:aws:ecr:{scope}:repository/fixture/{workload}": "registry"
            for workload in ("controller", "evaluator", "trainer")
        },
        parameters["certificate_arn"]: "certificate",
    }
    return {
        arn: ResourceBinding(
            arn=arn,
            kind=kind,
            environment=parameters["environment_id"],
            account_id=parameters["account_id"],
            region=parameters["region"],
            configuration_digest="2" * 64,
        )
        for arn, kind in references.items()
    }


@pytest.fixture
def preparation_inputs(
    parameters, environment_identity, approved_profiles, approved_release, inventory
):
    return {
        "requested": parameters,
        "approved_identity": environment_identity,
        "profiles": approved_profiles,
        "release": approved_release,
        "inventory": inventory,
    }


@pytest.fixture
def prepare(preparation_inputs):
    def run(**overrides):
        return prepare_cell_install(**(preparation_inputs | overrides))

    return run


@pytest.mark.parametrize("mode", ["opentour_managed", "customer_controlled"])
def test_verified_template_preserves_exact_shared_bytes(
    prepare, parameters, environment_identity, approved_release, mode
):
    template = b'{"AWSTemplateFormatVersion":"2010-09-09","Resources":{}}\n'
    parameters["mode"] = environment_identity["mode"] = mode
    release = replace(approved_release, template_sha256=hashlib.sha256(template).hexdigest())
    prepared = prepare(release=release)

    assert prepared.verified_template(template) == template
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_TEMPLATE_MISMATCH$"):
        prepared.verified_template(template.rstrip())


def test_verified_template_refuses_empty_release_template(prepare, approved_release):
    release = replace(approved_release, template_sha256=hashlib.sha256(b"").hexdigest())
    prepared = prepare(release=release)

    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_TEMPLATE_MISMATCH$"):
        prepared.verified_template(b"")


def test_verified_template_refuses_configuration_substitution(prepare, approved_release):
    template = b'{"AWSTemplateFormatVersion":"2010-09-09","Resources":{}}'
    template_sha256 = hashlib.sha256(template).hexdigest()
    prepared = prepare()
    replaced_configuration = prepared.configuration.replace(
        approved_release.template_sha256.encode(), template_sha256.encode()
    )
    tampered = replace(prepared, configuration=replaced_configuration)

    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_CONFIGURATION_INVALID$"):
        tampered.verified_template(template)


@pytest.mark.parametrize("mode", ["opentour_managed", "customer_controlled"])
def test_prepares_exact_approved_scope_and_immutable_input(
    prepare, parameters, environment_identity, approved_profiles, mode
):
    parameters.update(mode=mode, network_profile=f"fixture-{mode}-network")
    environment_identity["mode"] = mode
    profiles = replace(
        approved_profiles,
        network=QualifiedProfile(parameters["network_profile"], "3" * 64),
    )
    result = prepare(profiles=profiles)
    assert result == prepare(profiles=profiles, requested=dict(reversed(list(parameters.items()))))
    assert result.configuration_sha256 == hashlib.sha256(result.configuration).hexdigest()
    configuration = json.loads(result.configuration)
    assert configuration["parameters"] == parameters
    assert configuration["profile_sha256"]["network_profile"] == "3" * 64
    assert len(configuration["resource_configuration_sha256"]) == 7
    assert configuration["images"]["trainer"].endswith(f"@sha256:{parameters['trainer_digest']}")
    parameters["subnet_ids"].clear()
    assert json.loads(result.configuration) == configuration


@pytest.mark.parametrize(
    ("field", "foreign"),
    [
        ("organization_id", "foreign-org"),
        ("environment_id", "foreign-env"),
        ("cell_id", "foreign-cell"),
        ("mode", "customer_controlled"),
        ("account_id", "000000000002"),
        ("region", "us-east-2"),
    ],
)
def test_refuses_rebinding_each_permanent_coordinate(prepare, parameters, field, foreign):
    parameters[field] = foreign
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_IDENTITY_MISMATCH$"):
        prepare()


@pytest.mark.parametrize("field", ["control_endpoint", "kms_policy_profile", "network_profile"])
def test_refuses_unqualified_registry_keys(prepare, parameters, field):
    parameters[field] = "forged-qualified"
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_PROFILE_UNQUALIFIED$"):
        prepare()


def test_refuses_profile_without_qualified_digest(prepare, approved_profiles):
    profiles = replace(
        approved_profiles, kms_policy=replace(approved_profiles.kms_policy, sha256="")
    )
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_PROFILE_UNQUALIFIED$"):
        prepare(profiles=profiles)


@pytest.mark.parametrize(
    ("field", "foreign"),
    [
        ("subnet_ids", ["subnet-00000001"]),
        ("security_group_ids", ["sg-00000001"]),
        ("certificate_arn", None),
        ("gateway_hostname", None),
        ("retention_policy_ref", {"policy_id": "fixture-retention", "revision": "foreign-rev"}),
    ],
)
def test_refuses_owner_unapproved_network_or_retention(prepare, parameters, field, foreign):
    parameters[field] = foreign
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_PROFILE_MISMATCH$"):
        prepare()


@pytest.mark.parametrize("foreign_scope", ["us-east-1:000000000002", "us-east-2:000000000001"])
def test_refuses_foreign_certificate_even_if_requested_and_profile_match(
    prepare, approved_profiles, parameters, foreign_scope
):
    _, resource = parameters["certificate_arn"].split(":certificate/", 1)
    certificate = f"arn:aws:acm:{foreign_scope}:certificate/{resource}"
    parameters["certificate_arn"] = certificate
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_RESOURCE_SCOPE_MISMATCH$"):
        prepare(profiles=replace(approved_profiles, certificate_arn=certificate))


@pytest.mark.parametrize(
    ("field", "foreign"),
    [
        ("account_id", "000000000002"),
        ("region", "us-east-2"),
        ("environment", "foreign-env"),
        ("kind", "public-ingress"),
        ("configuration_digest", ""),
    ],
)
def test_refuses_inventory_entry_from_wrong_scope(prepare, inventory, field, foreign):
    arn = next(iter(inventory))
    inventory[arn] = replace(inventory[arn], **{field: foreign})
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_RESOURCE_UNQUALIFIED$"):
        prepare()


def test_refuses_missing_inventory_artifact(prepare, inventory):
    del inventory[next(iter(inventory))]
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_RESOURCE_UNQUALIFIED$"):
        prepare()


@pytest.mark.parametrize("reason", ["same_digest_foreign_ecr", "wrong_workload", "mutable_tag"])
def test_refuses_unapproved_image_location_or_digest(prepare, approved_release, reason):
    images = dict(approved_release.images)
    match reason:
        case "same_digest_foreign_ecr":
            images["trainer"] = images["trainer"].replace("000000000001.", "000000000002.")
        case "wrong_workload":
            images["trainer"] = images["evaluator"]
        case "mutable_tag":
            images["trainer"] = images["trainer"].split("@", 1)[0] + ":latest"
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_IMAGE_MISMATCH$"):
        prepare(release=replace(approved_release, images=images))


@pytest.mark.parametrize("field", ["release_sha256", "template_sha256", "parameter_schema_sha256"])
def test_refuses_missing_release_binding(prepare, approved_release, field):
    with pytest.raises(
        CellInstallRefused, match="^CELL_INSTALL_(RELEASE_INVALID|SCHEMA_MISMATCH)$"
    ):
        prepare(release=replace(approved_release, **{field: ""}))


def test_refuses_invalid_trusted_identity_or_unknown_parameter(prepare, parameters):
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_IDENTITY_INVALID$"):
        prepare(approved_identity={"environment_id": "fixture-environment"})
    parameters["secret"] = "GENERATED_SENTINEL"
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_PARAMETERS_INVALID$"):
        prepare()


@pytest.fixture
def key_policies():
    return {}


@pytest.fixture
def approved_storage(parameters, approved_profiles, inventory, key_policies) -> ApprovedCellStorage:
    account, region = parameters["account_id"], parameters["region"]

    def register(arn, kind):
        inventory[arn] = ResourceBinding(
            arn, kind, parameters["environment_id"], account, region, "3" * 64
        )

    def key(key_id, denied_role=None):
        key_arn = f"arn:aws:kms:{region}:{account}:key/00000000-0000-0000-0000-{key_id:012d}"
        if denied_role is not None:
            statement = {
                "Sid": f"DenyOtherRole{key_id}",
                "Effect": "Deny",
                "Principal": {"AWS": "*"},
                "Action": ["kms:Decrypt", "kms:ReEncryptFrom"],
                "Resource": "*",
                "Condition": {"ArnEquals": {"aws:PrincipalArn": denied_role}},
            }
        else:
            statement = {
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account}:role/generated/artifact-reader"},
                "Action": "kms:Decrypt",
                "Resource": "*",
            }
        policy = json.dumps({"Version": "2012-10-17", "Statement": [statement]}).encode()
        key_policies[key_arn] = policy
        register(key_arn, "key")
        return RetainedStorageKeyBinding(key_arn, hashlib.sha256(policy).hexdigest())

    trainer_role = f"arn:aws:iam::{account}:role/generated/trainer"
    evaluator_role = f"arn:aws:iam::{account}:role/generated/evaluator"
    register(trainer_role, "role")
    register(evaluator_role, "role")
    trainer_key = key(1, evaluator_role)
    evaluator_key = key(2, trainer_role)
    return ApprovedCellStorage(
        kms_policy_sha256=approved_profiles.kms_policy.sha256,
        trainer=WorkloadStorageBinding(
            trainer_key.kms_key_arn, trainer_role, trainer_key.key_policy_sha256
        ),
        evaluator=WorkloadStorageBinding(
            evaluator_key.kms_key_arn, evaluator_role, evaluator_key.key_policy_sha256
        ),
        evidence=key(3, trainer_role),
        artifact=key(4),
    )


@pytest.fixture
def compile_storage(preparation_inputs, approved_storage, key_policies):
    def run(**overrides):
        return compile_cell_storage(
            **(
                preparation_inputs
                | {"storage": approved_storage, "key_policies": key_policies}
                | overrides
            )
        )

    return run


@pytest.mark.parametrize(
    ("logical_id", "own", "opposite"),
    [
        ("TrainerStorage", "trainer", "evaluator"),
        ("EvaluatorStorage", "evaluator", "trainer"),
        ("EvidenceStorage", "evidence", "trainer"),
        ("ArtifactStorage", "artifact", None),
    ],
)
def test_compiled_storage_retains_private_encrypted_buckets_and_policies(
    compile_storage, approved_storage, logical_id, own, opposite
):
    compiled = compile_storage()
    assert compiled == compile_storage()
    assert compiled.configuration_sha256 == hashlib.sha256(compiled.configuration).hexdigest()
    configuration = json.loads(compiled.configuration)
    resources = configuration["cloudformation_resources"]
    assert set(resources) == {
        "TrainerStorage",
        "TrainerStoragePolicy",
        "EvaluatorStorage",
        "EvaluatorStoragePolicy",
        "EvidenceStorage",
        "EvidenceStoragePolicy",
        "ArtifactStorage",
        "ArtifactStoragePolicy",
    }
    assert {item["Type"] for item in resources.values()} == {
        "AWS::S3::Bucket",
        "AWS::S3::BucketPolicy",
    }
    bucket, policy = resources[logical_id], resources[f"{logical_id}Policy"]
    for resource in (bucket, policy):
        assert resource["DeletionPolicy"] == resource["UpdateReplacePolicy"] == "Retain"
    properties = bucket["Properties"]
    assert "BucketName" not in properties
    assert "LifecycleConfiguration" not in properties
    assert properties["PublicAccessBlockConfiguration"] == dict.fromkeys(
        ["BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"], True
    )
    assert properties["VersioningConfiguration"] == {"Status": "Enabled"}
    assert properties["OwnershipControls"] == {
        "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
    }
    key = getattr(approved_storage, own).kms_key_arn
    assert properties["BucketEncryption"]["ServerSideEncryptionConfiguration"] == [
        {
            "BucketKeyEnabled": False,
            "ServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": key},
        }
    ]
    assert policy["Properties"]["Bucket"] == {"Ref": logical_id}
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    assert all(
        statement["Effect"] == "Deny" and statement["Principal"] == "*" for statement in statements
    )
    object_arn = {"Fn::Sub": "${" + logical_id + ".Arn}/*"}
    if opposite is not None:
        other_role = getattr(approved_storage, opposite).role_arn
        assert statements[0] == {
            "Sid": "DenyOppositeWorkload",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [{"Fn::GetAtt": [logical_id, "Arn"]}, object_arn],
            "Condition": {"ArnEquals": {"aws:PrincipalArn": other_role}},
        }
        assert other_role in configuration["resource_configuration_sha256"]
        statements = statements[1:]
    assert [statement["Action"] for statement in statements] == ["s3:PutObject"] * 2
    assert all(statement["Resource"] == object_arn for statement in statements)
    assert [statement["Condition"] for statement in statements] == [
        {"StringNotEquals": {"s3:x-amz-server-side-encryption": "aws:kms"}},
        {"StringNotEquals": {"s3:x-amz-server-side-encryption-aws-kms-key-id": key}},
    ]
    assert key in configuration["resource_configuration_sha256"]


def test_storage_resources_use_the_same_compiler_in_both_modes(
    compile_storage, parameters, environment_identity, approved_profiles
):
    managed = json.loads(compile_storage().configuration)
    parameters.update(
        mode="customer_controlled",
        network_profile="fixture-customer-controlled-network",
        gateway_hostname=None,
        certificate_arn=None,
    )
    environment_identity["mode"] = parameters["mode"]
    profiles = replace(
        approved_profiles,
        network=QualifiedProfile(parameters["network_profile"], "3" * 64),
        gateway_hostname=None,
        certificate_arn=None,
    )
    customer = json.loads(compile_storage(profiles=profiles).configuration)
    assert managed["cloudformation_resources"] == customer["cloudformation_resources"]
    assert managed["kms_key_policy_sha256"] == customer["kms_key_policy_sha256"]
    assert managed["parameters"]["network_profile"] != customer["parameters"]["network_profile"]


def test_checked_in_retained_storage_matches_policy_builder():
    template = read_json(CELL_TEMPLATE)
    for section, expected in retained_storage_template_section().items():
        assert {name: template[section][name] for name in expected} == expected


def _resolve_storage_parameters(value, bindings):
    if isinstance(value, dict):
        if set(value) == {"Ref"} and value["Ref"] in bindings:
            return bindings[value["Ref"]]
        return {key: _resolve_storage_parameters(item, bindings) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_storage_parameters(item, bindings) for item in value]
    return value


@pytest.mark.parametrize("mode", ["opentour_managed", "customer_controlled"])
def test_common_template_retained_storage_matches_compiled_configuration(
    compile_storage,
    parameters,
    environment_identity,
    approved_profiles,
    approved_release,
    approved_storage,
    mode,
):
    template_bytes = CELL_TEMPLATE.read_bytes()
    release = replace(approved_release, template_sha256=hashlib.sha256(template_bytes).hexdigest())
    profiles = approved_profiles
    if mode == "customer_controlled":
        parameters.update(
            mode=mode,
            network_profile="fixture-customer-controlled-network",
            gateway_hostname=None,
            certificate_arn=None,
        )
        environment_identity["mode"] = mode
        profiles = replace(
            profiles,
            network=QualifiedProfile(parameters["network_profile"], "3" * 64),
            gateway_hostname=None,
            certificate_arn=None,
        )

    prepared = compile_storage(release=release, profiles=profiles)
    template = json.loads(prepared.verified_template(template_bytes))
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_TEMPLATE_MISMATCH$"):
        prepared.verified_template(template_bytes + b" ")
    resources = json.loads(prepared.configuration)["cloudformation_resources"]
    bindings = {
        "TrainerStorageKmsKeyArn": approved_storage.trainer.kms_key_arn,
        "EvaluatorStorageKmsKeyArn": approved_storage.evaluator.kms_key_arn,
        "EvidenceStorageKmsKeyArn": approved_storage.evidence.kms_key_arn,
        "ArtifactStorageKmsKeyArn": approved_storage.artifact.kms_key_arn,
        "TrainerRoleArn": approved_storage.trainer.role_arn,
        "EvaluatorRoleArn": approved_storage.evaluator.role_arn,
    }
    assert set(bindings).isdisjoint(parameters)
    for name, arn in bindings.items():
        definition = template["Parameters"][name]
        assert definition["Type"] == "String" and "Default" not in definition
        assert re.fullmatch(definition["AllowedPattern"], arn)
    storage = {name: template["Resources"][name] for name in resources}
    assert _resolve_storage_parameters(storage, bindings) == resources


def test_generated_storage_fragment_passes_bundled_cloudformation_schema(compile_storage, tmp_path):
    configuration = json.loads(compile_storage().configuration)
    # This local envelope exists only to run the pinned resource-schema checker.
    template = tmp_path / "generated-storage-fragment.json"
    template.write_text(
        json.dumps({"Resources": configuration["cloudformation_resources"]}), encoding="utf-8"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(Path(sys.executable).with_name("cfn-lint")),
            "--format",
            "json",
            "--regions",
            "us-east-1",
            "--template",
            str(template),
        ],
        timeout=30,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (result.stdout.decode(), result.stderr.decode())
    assert json.loads(result.stdout) == []


@pytest.mark.parametrize("field", ["kms_policy_sha256", "role_arn", "kms_key_arn"])
def test_refuses_unqualified_profile_or_shared_workload_authority(
    compile_storage, approved_storage, field
):
    if field == "kms_policy_sha256":
        binding = replace(approved_storage, kms_policy_sha256="4" * 64)
    else:
        binding = replace(
            approved_storage,
            evaluator=replace(
                approved_storage.evaluator, **{field: getattr(approved_storage.trainer, field)}
            ),
        )
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_STORAGE_BINDING_MISMATCH$"):
        compile_storage(storage=binding)


@pytest.mark.parametrize(
    ("store", "reused_key"), [("evidence", "trainer"), ("artifact", "evidence")]
)
def test_refuses_reuse_of_retained_data_keys(compile_storage, approved_storage, store, reused_key):
    original = getattr(approved_storage, store)
    reused = getattr(approved_storage, reused_key)
    storage = replace(
        approved_storage, **{store: replace(original, kms_key_arn=reused.kms_key_arn)}
    )
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_STORAGE_BINDING_MISMATCH$"):
        compile_storage(storage=storage)


@pytest.mark.parametrize("evaluator_name", ["generated/TRAINER", "other/trainer"])
def test_refuses_workload_role_name_reuse_across_case_or_path(
    compile_storage, approved_storage, evaluator_name
):
    role_prefix = approved_storage.trainer.role_arn.split(":role/", 1)[0]
    evaluator_role = f"{role_prefix}:role/{evaluator_name}"
    binding = replace(
        approved_storage,
        evaluator=replace(approved_storage.evaluator, role_arn=evaluator_role),
    )

    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_STORAGE_BINDING_MISMATCH$"):
        compile_storage(storage=binding)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role_arn", "arn:aws:iam::000000000002:role/generated/trainer"),
        ("role_arn", "arn:aws:iam::000000000001:user/generated/trainer"),
        ("role_arn", "arn:aws:sts::000000000001:assumed-role/generated/trainer"),
        ("role_arn", "arn:aws:iam::000000000001:role/generated/*"),
        (
            "kms_key_arn",
            "arn:aws:kms:us-east-2:000000000001:key/00000000-0000-0000-0000-000000000001",
        ),
        (
            "kms_key_arn",
            "arn:aws:kms:us-east-1:000000000002:key/00000000-0000-0000-0000-000000000001",
        ),
        ("kms_key_arn", "arn:aws:kms:us-east-1:000000000001:alias/generated"),
        (
            "kms_key_arn",
            "arn:aws:kms:us-east-1:000000000001:key/mrk-00000000000000000000000000000000",
        ),
    ],
)
def test_refuses_foreign_or_noncanonical_storage_scope(
    compile_storage, approved_storage, field, value
):
    storage = replace(approved_storage, trainer=replace(approved_storage.trainer, **{field: value}))
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_STORAGE_SCOPE_MISMATCH$"):
        compile_storage(storage=storage)


@pytest.mark.parametrize(
    ("store", "field"),
    [
        ("trainer", "role_arn"),
        ("trainer", "kms_key_arn"),
        ("evidence", "kms_key_arn"),
        ("artifact", "kms_key_arn"),
    ],
)
def test_storage_requires_current_scoped_inventory(
    compile_storage, approved_storage, inventory, store, field
):
    arn = getattr(getattr(approved_storage, store), field)
    original = inventory.pop(arn)
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_RESOURCE_UNQUALIFIED$"):
        compile_storage()
    inventory[arn] = replace(original, environment="other-cell-environment")
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_RESOURCE_UNQUALIFIED$"):
        compile_storage()
    inventory[arn] = replace(original, configuration_digest="7" * 64)
    changed = compile_storage()
    inventory[arn] = original
    assert changed.configuration_sha256 != compile_storage().configuration_sha256


@pytest.mark.parametrize(
    ("store", "old", "new"),
    [("evidence", "us-east-1", "us-east-2"), ("artifact", "000000000001", "000000000002")],
)
def test_refuses_retained_key_from_foreign_scope(
    compile_storage, approved_storage, store, old, new
):
    key = getattr(approved_storage, store)
    storage = replace(
        approved_storage, **{store: replace(key, kms_key_arn=key.kms_key_arn.replace(old, new, 1))}
    )
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_STORAGE_SCOPE_MISMATCH$"):
        compile_storage(storage=storage)


def test_storage_cannot_bypass_base_installer_admission(compile_storage, parameters):
    parameters["cell_id"] = "other-cell"
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_IDENTITY_MISMATCH$"):
        compile_storage()


def test_storage_binds_all_key_policy_digests(compile_storage, key_policies):
    compiled = compile_storage()
    assert json.loads(compiled.configuration)["kms_key_policy_sha256"] == {
        arn: hashlib.sha256(policy).hexdigest() for arn, policy in key_policies.items()
    }
    assert b"DenyOtherRole" not in compiled.configuration


@pytest.mark.parametrize("store", ["trainer", "evidence", "artifact"])
@pytest.mark.parametrize("case", ["missing", "swapped", "changed"])
def test_storage_refuses_missing_or_unapproved_key_policy_readback(
    compile_storage, approved_storage, key_policies, case, store
):
    key = getattr(approved_storage, store)
    evaluator_key = approved_storage.evaluator.kms_key_arn
    if case == "missing":
        del key_policies[key.kms_key_arn]
    elif case == "swapped":
        key_policies[key.kms_key_arn] = key_policies[evaluator_key]
    else:
        key_policies[key.kms_key_arn] += b" "
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_KEY_POLICY_UNQUALIFIED$"):
        compile_storage()


@pytest.mark.parametrize(
    "change", ["allow", "only_decrypt", "wrong_role", "conditional", "wrong_resource", "not_action"]
)
@pytest.mark.parametrize("store", ["evaluator", "evidence"])
def test_protected_storage_requires_unqualified_trainer_decrypt_deny(
    compile_storage, approved_storage, key_policies, change, store
):
    key = getattr(approved_storage, store)
    policy = json.loads(key_policies[key.kms_key_arn])
    statement = policy["Statement"][0]
    match change:
        case "allow":
            statement["Effect"] = "Allow"
        case "only_decrypt":
            statement["Action"] = "kms:Decrypt"
        case "wrong_role":
            statement["Condition"] = {
                "ArnEquals": {"aws:PrincipalArn": approved_storage.evaluator.role_arn}
            }
        case "conditional":
            statement["Condition"]["StringEquals"] = {
                "kms:ViaService": "s3.us-east-1.amazonaws.com"
            }
        case "wrong_resource":
            statement["Resource"] = approved_storage.trainer.kms_key_arn
        case "not_action":
            statement["NotAction"] = statement.pop("Action")
    encoded = json.dumps(policy).encode()
    key_policies[key.kms_key_arn] = encoded
    key = replace(key, key_policy_sha256=hashlib.sha256(encoded).hexdigest())
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_CROSS_ROLE_DECRYPT_DENY_MISSING$"):
        compile_storage(storage=replace(approved_storage, **{store: key}))


def test_storage_refuses_ambiguous_key_policy_json(compile_storage, approved_storage, key_policies):
    trainer = approved_storage.trainer
    encoded = key_policies[trainer.kms_key_arn].replace(
        b'"Effect": "Deny"', b'"Effect": "Allow", "Effect": "Deny"'
    )
    key_policies[trainer.kms_key_arn] = encoded
    trainer = replace(trainer, key_policy_sha256=hashlib.sha256(encoded).hexdigest())
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_KEY_POLICY_UNQUALIFIED$"):
        compile_storage(storage=replace(approved_storage, trainer=trainer))


@pytest.mark.parametrize("statements", [[], [None], {"Effect": "Allow"}])
def test_artifact_key_policy_requires_statements_even_with_a_matching_digest(
    compile_storage, approved_storage, key_policies, statements
):
    key = approved_storage.artifact
    encoded = json.dumps({"Version": "2012-10-17", "Statement": statements}).encode()
    key_policies[key.kms_key_arn] = encoded
    key = replace(key, key_policy_sha256=hashlib.sha256(encoded).hexdigest())
    with pytest.raises(CellInstallRefused, match="^CELL_INSTALL_KEY_POLICY_UNQUALIFIED$"):
        compile_storage(storage=replace(approved_storage, artifact=key))
