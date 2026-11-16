"""OPE-213 topology/security assertions over actual templates; AWS gates stay separate."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from infrastructure.environment_contract import (
    BASELINE_COST_COMPONENTS,
    EnvironmentError,
    connection_ceiling,
    security_configuration_diff,
    validate_cost_inventory,
)
from infrastructure.tests.environment_fixtures import TEMPLATES, template


def test_real_cloudformation_resource_schema_validation() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(Path(sys.executable).with_name("cfn-lint")),
            "--format",
            "json",
            "--regions",
            "us-east-1",
            "--template",
            str(TEMPLATES / "control-plane.yaml"),
            str(TEMPLATES / "environment-boundaries.yaml"),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout.decode()
    assert json.loads(result.stdout) == []


def test_two_az_private_database_backup_and_network_topology() -> None:
    resources = template("control-plane")["Resources"]
    for group in ("Public", "Application", "Database"):
        assert {
            json.dumps(resources[group + suffix]["Properties"]["AvailabilityZone"])
            for suffix in ("A", "B")
        } == {json.dumps({"Fn::Select": [i, {"Fn::GetAZs": ""}]}) for i in (0, 1)}
    database = resources["ControlDatabase"]["Properties"]
    assert database["PubliclyAccessible"] is False
    assert database["StorageEncrypted"] is True
    assert database["MultiAZ"] == {"Fn::If": ["IsProduction", True, False]}
    assert database["BackupRetentionPeriod"] == 7
    assert database["EnableIAMDatabaseAuthentication"] is True
    assert resources["ControlDatabase"]["DeletionPolicy"] == "Retain"
    assert database["VPCSecurityGroups"] == [{"Ref": "DatabaseSecurityGroup"}]
    assert resources["DatabaseParameters"]["Properties"]["Parameters"]["rds.force_ssl"] == "1"
    assert resources["PrivateRoutes"]["Type"] == "AWS::EC2::RouteTable"
    routes = [r["Properties"] for r in resources.values() if r["Type"] == "AWS::EC2::Route"]
    assert routes == [
        {
            "RouteTableId": {"Ref": "PublicRoutes"},
            "DestinationCidrBlock": "0.0.0.0/0",
            "GatewayId": {"Ref": "InternetGateway"},
        }
    ]
    assert not any(r["Type"] == "AWS::EC2::NatGateway" for r in resources.values())
    for name in ("ApplicationA", "ApplicationB", "DatabaseA", "DatabaseB"):
        assert resources[name]["Properties"]["MapPublicIpOnLaunch"] is False


def test_ingress_and_egress_have_explicit_narrow_paths() -> None:
    resources = template("control-plane")["Resources"]
    listener = resources["TlsListener"]["Properties"]
    assert (listener["Port"], listener["Protocol"]) == (443, "HTTPS")
    assert listener["DefaultActions"][0]["FixedResponseConfig"]["StatusCode"] == "503"
    for name in ("ApplicationSecurityGroup", "DatabaseSecurityGroup", "EndpointSecurityGroup"):
        properties = resources[name]["Properties"]
        assert properties.get("SecurityGroupIngress", []) == []
        assert properties["SecurityGroupEgress"][0]["CidrIp"] == "127.0.0.1/32"
    ingress = [
        r["Properties"] for r in resources.values() if r["Type"] == "AWS::EC2::SecurityGroupIngress"
    ]
    assert sorted(p["FromPort"] for p in ingress) == [443, 3000, 5432, 8000]
    assert all(p["FromPort"] == p["ToPort"] and "SourceSecurityGroupId" in p for p in ingress)
    egress = [
        r["Properties"] for r in resources.values() if r["Type"] == "AWS::EC2::SecurityGroupEgress"
    ]
    assert all("DestinationSecurityGroupId" in p or "DestinationPrefixListId" in p for p in egress)
    assert resources["EcrS3Egress"]["Properties"]["DestinationPrefixListId"] == {
        "Ref": "EcrS3PrefixListId"
    }
    endpoints = [r for r in resources.values() if r["Type"] == "AWS::EC2::VPCEndpoint"]
    assert len(endpoints) == 7
    assert resources["S3Endpoint"]["Properties"]["PolicyDocument"]["Statement"][0]["Action"] == [
        "s3:GetObject"
    ]


def test_services_are_separate_inactive_digest_pinned_and_capacity_bounded() -> None:
    model = template("control-plane")
    resources = model["Resources"]
    assert model["Parameters"]["ApplicationEnabled"]["Default"] == "false"
    roles = []
    for name in ("Bff", "Api", "Worker"):
        task = resources[name + "Task"]
        service = resources[name + "Service"]
        assert task["Condition"] == service["Condition"] == "RunApplication"
        roles.append(task["Properties"]["TaskRoleArn"])
        assert task["Properties"]["ContainerDefinitions"][0]["ReadonlyRootFilesystem"] is True
        assert "@sha256:" in model["Parameters"][name + "ImageDigest"]["AllowedPattern"]
        assert service["Properties"]["DesiredCount"] == 0
        assert service["Properties"]["EnableExecuteCommand"] is False
        assert (
            service["Properties"]["NetworkConfiguration"]["AwsvpcConfiguration"]["AssignPublicIp"]
            == "DISABLED"
        )
        # Rolling deployment cannot double the configured DB connection ceiling.
        assert service["Properties"]["DeploymentConfiguration"]["MaximumPercent"] == 100
        assert resources[name + "Repository"]["Properties"]["ImageTagMutability"] == "IMMUTABLE"
    assert len({json.dumps(role) for role in roles}) == 3
    scaling = resources["ApiScalingTarget"]["Properties"]
    assert scaling["MaxCapacity"] == {"Ref": "ApiMaxInstances"}
    assert scaling["MinCapacity"] == 0
    assert all(scaling["SuspendedState"].values())
    assert model["Parameters"]["ApiPoolPerInstance"]["Default"] == 20


def test_runtime_roles_are_not_provisioners_schema_owners_or_audit_administrators() -> None:
    resources = template("environment-boundaries")["Resources"]
    allowed = {
        "Bff": {"secretsmanager:GetSecretValue", "kms:Decrypt"},
        "Api": {
            "secretsmanager:GetSecretValue",
            "kms:Decrypt",
            "kms:GenerateDataKey",
            "rds-db:connect",
            "sqs:SendMessage",
            "sqs:GetQueueAttributes",
        },
        "Worker": {
            "rds-db:connect",
            "sqs:ReceiveMessage",
            "sqs:DeleteMessage",
            "sqs:ChangeMessageVisibility",
            "sqs:GetQueueAttributes",
            "kms:Decrypt",
        },
        "MigrationTask": {"rds-db:connect"},
        "TaskExecution": {
            "ecr:GetAuthorizationToken",
            "ecr:BatchGetImage",
            "ecr:GetDownloadUrlForLayer",
            "ecr:BatchCheckLayerAvailability",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
        },
    }
    for name, expected in allowed.items():
        role = resources[name + "Role"]["Properties"]
        assert role["PermissionsBoundary"] == {"Ref": name + "Boundary"}
        policy = role["Policies"][0]["PolicyDocument"]
        assert policy == resources[name + "Boundary"]["Properties"]["PolicyDocument"]
        assert {a for s in policy["Statement"] for a in s["Action"]} == expected
        for statement in policy["Statement"]:
            assert statement["Effect"] == "Allow"
            if statement["Resource"] == "*":
                assert statement["Action"] == ["ecr:GetAuthorizationToken"]
        trust = role["AssumeRolePolicyDocument"]["Statement"][0]
        assert trust["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}
        assert trust["Condition"]["StringEquals"]["aws:SourceAccount"] == {"Ref": "AWS::AccountId"}
    deploy = resources["DeployRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert deploy[0]["Resource"] == {"Ref": "DeclaredServiceArns"}
    assert deploy[1]["Action"] == ["iam:PassRole"]
    assert len(deploy[1]["Resource"]) == 4
    assert (
        deploy[1]["Condition"]["StringEquals"]["iam:PassedToService"] == "ecs-tasks.amazonaws.com"
    )
    assert not any("Audit" in name for name in resources)


def test_oidc_trust_requires_every_exact_identity_claim() -> None:
    resources = template("environment-boundaries")["Resources"]
    for name, workflow in (
        ("Deploy", "ApprovedDeployWorkflow"),
        ("Migration", "ApprovedMigrationWorkflow"),
    ):
        trust = resources[name + "Role"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        assert trust["Action"] == "sts:AssumeRoleWithWebIdentity"
        conditions = trust["Condition"]
        assert conditions["StringEquals"] == {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": {
                "Fn::Sub": (
                    "repo:kevon-dot@276088488/opentour-ai@1455972360:environment:${Environment}"
                )
            },
            "token.actions.githubusercontent.com:repository_id": "1455972360",
            "token.actions.githubusercontent.com:job_workflow_ref": {"Ref": workflow},
            "token.actions.githubusercontent.com:ref": "refs/heads/main",
            "token.actions.githubusercontent.com:environment": {"Ref": "Environment"},
        }
        assert conditions["NumericLessThanEquals"] == {"sts:DurationSeconds": 900}
        assert "StringLike" not in conditions
        subject = conditions["StringEquals"]["token.actions.githubusercontent.com:sub"]["Fn::Sub"]
        for environment in ("dev", "test", "prod"):
            expected = subject.replace("${Environment}", environment)
            assert expected == (
                "repo:kevon-dot@276088488/opentour-ai@1455972360:environment:" + environment
            )
            for rejected in (
                "repo:kevon-dot/opentour-ai:environment:" + environment,
                expected.replace("@276088488", "@000000000"),
                expected.replace("@1455972360", "@0000000000"),
                expected.replace("repo:kevon-dot@", "repo:generated-fork@"),
            ):
                assert expected != rejected


@pytest.mark.parametrize("maximum", [1, 2, 3, 4])
def test_connection_ceiling_exact_fit(maximum: int) -> None:
    assert connection_ceiling(100, 5, 5, 10, requested_max_instances=maximum) == 4


@pytest.mark.parametrize(
    "kwargs",
    [
        {"requested_max_instances": 5},
        {"requested_max_instances": True},
        {"requested_max_instances": 1, "pool_per_api_instance": 0},
        {"requested_max_instances": 1, "pool_per_api_instance": 100},
        {"requested_max_instances": 1.0},
        {"requested_max_instances": 2**31},
    ],
)
def test_connection_budget_refuses_unsafe_or_coerced_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(EnvironmentError):
        connection_ceiling(100, 5, 5, 10, **kwargs)


@pytest.mark.parametrize(
    ("kind", "property", "before", "after"),
    [
        ("AWS::ECS::TaskDefinition", "TaskRoleArn", "scoped", "administrator"),
        ("AWS::RDS::DBInstance", "PubliclyAccessible", False, True),
        (
            "AWS::SQS::QueuePolicy",
            "PolicyDocument",
            {"Action": "sqs:SendMessage"},
            {"Action": "sqs:*"},
        ),
        ("AWS::S3::BucketPolicy", "PolicyDocument", {"Principal": "scoped"}, {"Principal": "*"}),
        ("AWS::EC2::SecurityGroupEgress", "CidrIp", "10.0.0.0/24", "0.0.0.0/0"),
    ],
)
def test_security_diff_surfaces_changed_privilege_and_egress_values(
    kind: str, property: str, before: Any, after: Any
) -> None:
    a = {"Resources": {"R": {"Type": kind, "Properties": {property: before}}}}
    b = {"Resources": {"R": {"Type": kind, "Properties": {property: after}}}}
    result = security_configuration_diff(a, b)
    assert result["security_review_required"] is True
    assert result["changes"][0]["security_changes"] == [
        {"property": property, "before": before, "after": after}
    ]
    assert result["provider_change_set"] == "NOT_RUN"


def test_parameter_diff_requires_review_without_exporting_secret_defaults() -> None:
    original = template("control-plane")
    changed = copy.deepcopy(original)
    changed["Parameters"]["Sentinel"] = {
        "Type": "String",
        "NoEcho": True,
        "Default": "generated-sensitive-sentinel",
    }
    diff = security_configuration_diff(original, changed)
    assert diff["security_review_required"] is True
    assert diff["configuration_changes"][0]["section"] == "Parameters"
    assert "generated-sensitive-sentinel" not in json.dumps(diff)
    assert security_configuration_diff(original, original)["changes"] == []


def test_complete_cost_inventory_includes_fixed_costs_and_explained_zero_nat() -> None:
    inventory = {
        key: {"quantity": 1, "unit": "configured_resource", "zero_reason": None}
        for key in BASELINE_COST_COMPONENTS
    }
    inventory["nat"] = {
        "quantity": 0,
        "unit": "gateway",
        "zero_reason": "No NAT in the reviewed empty topology",
    }
    validate_cost_inventory(inventory)
    for missing in ("alb", "rds", "nat", "vpc_endpoints", "logs", "audit_storage"):
        altered = inventory.copy()
        del altered[missing]
        with pytest.raises(EnvironmentError):
            validate_cost_inventory(altered)
    inventory["nat"]["zero_reason"] = None
    with pytest.raises(EnvironmentError):
        validate_cost_inventory(inventory)
