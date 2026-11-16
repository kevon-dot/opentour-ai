"""OPE-213 AC01/02/03: offline boundary checks, not provider qualification."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from infrastructure.environment_contract import (
    EnvironmentError,
    ResourceBinding,
    prepare_stack,
    validate_manifest,
)
from infrastructure.tests.environment_fixtures import (
    NONPROD,
    PROD,
    REGION,
    control_inputs,
    fixture,
    rename_reference,
    seal,
    template_digest,
)


@pytest.mark.parametrize("environment", ["dev", "test", "prod"])
def test_approved_environment_manifest(environment: str) -> None:
    manifest, policy, inventory = fixture(environment)
    verified = validate_manifest(manifest, policy, inventory)
    assert verified.account_id == (PROD if environment == "prod" else NONPROD)
    assert verified.environment == environment
    assert verified.configuration_digest == manifest["configuration_digest"]


def test_dev_and_test_share_account_without_sharing_authority() -> None:
    dev, policy, inventory = fixture("dev")
    test, _, other = fixture("test")
    inventory.update(other)
    dev["secrets_refs"] = test["secrets_refs"]
    seal(dev)
    with pytest.raises(EnvironmentError, match="CROSS_ENVIRONMENT_REFERENCE"):
        validate_manifest(dev, policy, inventory)


@pytest.mark.parametrize(
    "field", ["key_refs", "secrets_refs", "database_ref", "network_policy_ref"]
)
def test_prod_reference_injected_into_dev_is_rejected(field: str) -> None:
    dev, policy, inventory = fixture()
    prod, _, other = fixture("prod")
    inventory.update(other)
    dev[field] = prod[field]
    seal(dev)
    with pytest.raises(EnvironmentError, match="CROSS_ENVIRONMENT_REFERENCE"):
        validate_manifest(dev, policy, inventory)


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("key_refs", "alias/dev"),
        ("database_ref", "snapshot/dev"),
        ("service_refs", "cluster/dev"),
        ("vpc_ref", "subnet/dev"),
        ("subnet_refs", "vpc/dev"),
        ("network_policy_ref", "route-table/dev"),
        ("ingress_ref", "targetgroup/dev"),
        ("registry_refs", "image/dev"),
    ],
)
def test_matching_account_and_inventory_do_not_substitute_for_resource_kind(
    field: str, wrong: str
) -> None:
    manifest, policy, inventory = fixture()
    old = manifest[field][0] if isinstance(manifest[field], list) else manifest[field]
    arn = old["arn"].rsplit(":", 1)[0] + ":" + wrong
    # RDS DB ARN has a resource colon; construct its service portion directly.
    arn = ":".join(old["arn"].split(":")[:5]) + ":" + wrong
    rename_reference(manifest, inventory, field, arn)
    with pytest.raises(EnvironmentError, match="RESOURCE_KIND_MISMATCH"):
        validate_manifest(manifest, policy, inventory)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "missing",
        "digest",
        "unapproved",
        "stale",
        "duplicate",
        "account",
        "region",
        "audit",
        "partition",
        "wildcard",
    ],
)
def test_closed_manifest_rejects_adverse_inputs(mutation: str) -> None:
    value, policy, inventory = fixture()
    ref = value["key_refs"][0]
    if mutation == "unknown":
        value["extra"] = True
    elif mutation == "missing":
        del value["vpc_ref"]
    elif mutation == "digest":
        value["configuration_digest"] = "f" * 64
    elif mutation == "unapproved":
        inventory.pop(ref["arn"])
    elif mutation == "stale":
        ref["configuration_digest"] = "f" * 64
    elif mutation == "duplicate":
        value["key_refs"].append(ref.copy())
    elif mutation == "account":
        value["account_id"] = PROD
    elif mutation == "region":
        value["region"] = "us-west-2"
    elif mutation == "audit":
        policy = replace(policy, approved_audit_arns=frozenset())
    elif mutation == "partition":
        rename_reference(
            value, inventory, "key_refs", ref["arn"].replace("arn:aws:", "arn:aws-cn:")
        )
    elif mutation == "wildcard":
        rename_reference(value, inventory, "key_refs", ref["arn"] + "*")
    if mutation != "digest":
        seal(value)
    with pytest.raises(EnvironmentError):
        validate_manifest(value, policy, inventory)


def test_audit_account_must_be_independent() -> None:
    value, policy, inventory = fixture()
    with pytest.raises(EnvironmentError, match="ACCOUNTS_NOT_ISOLATED"):
        validate_manifest(value, replace(policy, audit_account=NONPROD), inventory)


def test_empty_control_stack_preflight_is_bound_to_exact_reviewed_bytes() -> None:
    parameters, policy, inventory = control_inputs()
    result = prepare_stack(
        "control-plane",
        parameters,
        policy,
        inventory,
        approved_template_sha256=template_digest("control-plane"),
        approved_ecr_s3_prefix_lists=frozenset(["pl-00000000"]),
    )
    assert result.provider_change_set == "NOT_RUN"
    assert json.loads(result.parameters_json)["ApplicationEnabled"] == "false"
    with pytest.raises(EnvironmentError, match="UNREVIEWED_TEMPLATE"):
        prepare_stack(
            "control-plane", parameters, policy, inventory, approved_template_sha256="f" * 64
        )


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("ExpectedAccountId", PROD),
        ("Unknown", "sentinel"),
        ("ApiMaxInstances", 5),
        ("DatabaseMaxConnections", True),
        ("DatabaseMaxConnections", 100.0),
        ("DatabaseEngineFamily", "postgres17"),
        ("VpcCidr", "0.0.0.0/0"),
        ("VpcCidr", "10.44.0.1/16"),
        ("ApplicationEnabled", "true"),
        ("BffImageDigest", "222222222222.dkr.ecr.us-east-1.amazonaws.com/app:latest"),
        ("EcrS3PrefixListId", "pl-deadbeef"),
    ],
)
def test_control_parameter_refusal_before_provider(parameter: str, value: Any) -> None:
    parameters, policy, inventory = control_inputs()
    parameters[parameter] = value
    with pytest.raises(EnvironmentError):
        prepare_stack(
            "control-plane",
            parameters,
            policy,
            inventory,
            approved_template_sha256=template_digest("control-plane"),
            approved_ecr_s3_prefix_lists=frozenset(["pl-00000000"]),
        )


def test_approved_application_inputs_and_foreign_role_refusal() -> None:
    parameters, policy, inventory = control_inputs()
    parameters["ApplicationEnabled"] = "true"
    images = set()
    for name in ("Bff", "Api", "Worker"):
        image = f"{NONPROD}.dkr.ecr.{REGION}.amazonaws.com/{name.lower()}@sha256:" + "a" * 64
        parameters[name + "ImageDigest"] = image
        images.add(image)
    for name in ("BffTaskRoleArn", "ApiTaskRoleArn", "WorkerTaskRoleArn", "TaskExecutionRoleArn"):
        arn = f"arn:aws:iam::{NONPROD}:role/dev-{name}"
        inventory[arn] = ResourceBinding(arn, "role", "dev", NONPROD, "", "a" * 64)
        parameters[name] = arn
    kwargs: dict[str, Any] = {
        "approved_template_sha256": template_digest("control-plane"),
        "approved_images": frozenset(images),
        "approved_ecr_s3_prefix_lists": frozenset(["pl-00000000"]),
    }
    assert (
        prepare_stack("control-plane", parameters, policy, inventory, **kwargs).environment == "dev"
    )
    arn = parameters["ApiTaskRoleArn"]
    inventory[arn] = replace(inventory[arn], environment="test")
    with pytest.raises(EnvironmentError, match="CROSS_ENVIRONMENT_REFERENCE"):
        prepare_stack("control-plane", parameters, policy, inventory, **kwargs)


def test_boundary_parameters_bind_exact_environment_resources() -> None:
    from infrastructure.tests.environment_fixtures import boundary_inputs

    parameters, policy, inventory = boundary_inputs()
    prepared = prepare_stack(
        "environment-boundaries",
        parameters,
        policy,
        inventory,
        approved_template_sha256=template_digest("environment-boundaries"),
    )
    assert prepared.provider_change_set == "NOT_RUN"
    assert len(json.loads(prepared.parameters_json)["DeclaredServiceArns"]) == 3


@pytest.mark.parametrize(
    "parameter", ["ApplicationSecretArn", "DataKeyArn", "BffLogGroupArn", "OidcProviderArn"]
)
def test_boundary_parameter_cross_environment_injection_is_refused(parameter: str) -> None:
    from infrastructure.tests.environment_fixtures import boundary_inputs

    parameters, policy, inventory = boundary_inputs()
    arn = parameters[parameter]
    inventory[arn] = replace(inventory[arn], environment="prod")
    with pytest.raises(EnvironmentError, match="CROSS_ENVIRONMENT_REFERENCE"):
        prepare_stack(
            "environment-boundaries",
            parameters,
            policy,
            inventory,
            approved_template_sha256=template_digest("environment-boundaries"),
        )


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("DatabaseResourceId", "db-UNAPPROVED"),
        ("DeclaredServiceArns", []),
        ("ApprovedDeployWorkflow", "kevon-dot/opentour-ai/.github/workflows/deploy.yml@main"),
        ("ApprovedMigrationWorkflow", "fork/other/.github/workflows/migrate.yml@" + "a" * 40),
    ],
)
def test_boundary_unapproved_database_services_or_workflows_refused(
    parameter: str, value: Any
) -> None:
    from infrastructure.tests.environment_fixtures import boundary_inputs

    parameters, policy, inventory = boundary_inputs()
    parameters[parameter] = value
    with pytest.raises(EnvironmentError):
        prepare_stack(
            "environment-boundaries",
            parameters,
            policy,
            inventory,
            approved_template_sha256=template_digest("environment-boundaries"),
        )
