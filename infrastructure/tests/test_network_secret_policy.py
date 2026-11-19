"""OPE-214 source checks for the dev private gateway policy. No provider calls."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from opentour_control.observability.policy import (
    PrivateGatewayBinding,
    PrivateGatewayPolicyError,
    check_private_gateway,
)

ROLE = "arn:aws:iam::111122223333:role/generated-bff"
API = "arn:aws:execute-api:us-east-2:111122223333:gateway"
TARGET = "arn:aws:elasticloadbalancing:us-east-2:111122223333:loadbalancer/net/generated/abcdef"
EXPORT_INTEGRATION = "x-amazon-apigateway-integration"
GATEWAY_TEMPLATE = (
    Path(__file__).resolve().parents[1] / "cloudformation" / "network-secrets-policy.yaml"
)
APPROVED_API_PATHS = {
    "/internal/browser-identity/v1/flows/consume",
    "/internal/browser-identity/v1/flows/create",
    "/internal/browser-identity/v1/sessions/admit",
    "/internal/browser-identity/v1/sessions/current",
    "/internal/browser-identity/v1/sessions/end",
}


def test_ohio_gateway_template_installs_only_the_approved_private_methods() -> None:
    template = json.loads(GATEWAY_TEMPLATE.read_text())
    resources = template["Resources"]
    api = resources["PrivateApi"]["Properties"]
    assert api["EndpointConfiguration"] == {
        "Types": ["PRIVATE"],
        "IpAddressType": "dualstack",
        "VpcEndpointIds": [{"Ref": "EndpointId"}],
    }
    statements = api["Policy"]["Statement"]
    assert len(statements) == 2
    assert all(
        statement["Effect"] == "Deny"
        and statement["Action"] == "execute-api:Invoke"
        and statement["Principal"] == "*"
        and statement["Resource"] == "execute-api:/*"
        for statement in statements
    )
    assert statements[0]["Condition"] == {
        "StringNotEquals": {"aws:SourceVpce": {"Ref": "EndpointId"}}
    }
    assert statements[1]["Condition"] == {
        "ArnNotEquals": {
            "aws:PrincipalArn": {
                "Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/${BffRolePath}"
            }
        }
    }

    def api_path(resource_id: str) -> str:
        properties = resources[resource_id]["Properties"]
        parent = properties["ParentId"]
        prefix = api_path(parent["Ref"]) if "Ref" in parent else ""
        return f"{prefix}/{properties['PathPart']}"

    methods = {
        name: definition["Properties"]
        for name, definition in resources.items()
        if definition["Type"] == "AWS::ApiGateway::Method"
    }
    assert len(methods) == len(APPROVED_API_PATHS)
    assert {
        api_path(method["ResourceId"]["Ref"]) for method in methods.values()
    } == APPROVED_API_PATHS
    for method in methods.values():
        path = api_path(method["ResourceId"]["Ref"])
        assert method["HttpMethod"] == "POST"
        assert method["AuthorizationType"] == "AWS_IAM"
        assert method["ApiKeyRequired"] is False
        assert method["Integration"] == {
            "Type": "HTTP_PROXY",
            "IntegrationHttpMethod": "POST",
            "ConnectionType": "VPC_LINK",
            "ConnectionId": {"Ref": "VpcLinkV2Id"},
            "IntegrationTarget": {
                "Fn::Sub": "arn:${AWS::Partition}:elasticloadbalancing:${AWS::Region}:"
                "${AWS::AccountId}:loadbalancer/net/${NetworkLoadBalancerNameAndId}"
            },
            "Uri": {"Fn::Sub": "https://${BackendServerName}:443/dev" + path},
        }
    assert set(resources["PrivateDeployment"]["DependsOn"]) == set(methods)


def test_ohio_stage_binds_the_deployment_and_has_no_request_logging_or_caching() -> None:
    template = json.loads(GATEWAY_TEMPLATE.read_text())
    assertion_conditions = [
        item["Assert"] for item in template["Rules"]["ApprovedOhioProject"]["Assertions"]
    ]
    assert assertion_conditions == [
        {"Fn::Equals": [{"Ref": "AWS::Region"}, "us-east-2"]},
        {"Fn::Equals": [{"Ref": "AWS::AccountId"}, {"Ref": "ExpectedProjectId"}]},
    ]
    assert template["Resources"]["DevStage"]["Properties"] == {
        "RestApiId": {"Ref": "PrivateApi"},
        "DeploymentId": {"Ref": "PrivateDeployment"},
        "ClientCertificateId": {"Ref": "ClientCertificate"},
        "StageName": "dev",
        "CacheClusterEnabled": False,
        "TracingEnabled": False,
        "MethodSettings": [
            {
                "ResourcePath": "/*",
                "HttpMethod": "*",
                "CachingEnabled": False,
                "DataTraceEnabled": False,
                "LoggingLevel": "OFF",
                "MetricsEnabled": False,
            }
        ],
    }


@pytest.fixture
def binding() -> PrivateGatewayBinding:
    return PrivateGatewayBinding(
        "111122223333",
        "us-east-2",
        "gateway",
        "deployment",
        "vpce-1234",
        "vpc-1234",
        ROLE,
        "link",
        TARGET,
        "identity.generated.example",
        "certificate",
        {"/generated-operation": "/generated-target"},
    )


@pytest.fixture
def readback() -> dict:
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Deny",
                "Principal": "*",
                "Action": "execute-api:Invoke",
                "Resource": f"{API}/*",
                "Condition": {"StringNotEquals": {"aws:SourceVpce": "vpce-1234"}},
            },
            {
                "Effect": "Deny",
                "Principal": "*",
                "Action": "execute-api:Invoke",
                "Resource": f"{API}/*",
                "Condition": {"ArnNotEquals": {"aws:PrincipalArn": ROLE}},
            },
        ],
    }
    return {
        "rest_api": {
            "id": "gateway",
            "endpointConfiguration": {"types": ["PRIVATE"], "vpcEndpointIds": ["vpce-1234"]},
            "policy": json.dumps(policy),
        },
        "endpoint": {
            "VpcEndpointId": "vpce-1234",
            "OwnerId": "111122223333",
            "VpcId": "vpc-1234",
            "VpcEndpointType": "Interface",
            "ServiceName": "com.amazonaws.us-east-2.execute-api",
            "PrivateDnsEnabled": False,
            "PolicyDocument": json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"AWS": ROLE},
                            "Action": "execute-api:Invoke",
                            "Resource": [f"{API}/dev/POST/generated-operation"],
                        }
                    ],
                }
            ),
        },
        "stage": {
            "stageName": "dev",
            "deploymentId": "deployment",
            "clientCertificateId": "certificate",
            "cacheClusterEnabled": False,
            "tracingEnabled": False,
            "methodSettings": {
                "*/*": {
                    "loggingLevel": "OFF",
                    "dataTraceEnabled": False,
                    "cachingEnabled": False,
                    "metricsEnabled": False,
                }
            },
        },
        "deployment": {
            "id": "deployment",
            "apiSummary": {
                "/generated-operation": {
                    "POST": {"authorizationType": "AWS_IAM", "apiKeyRequired": False}
                }
            },
        },
        "stage_export": {
            "paths": {
                "/generated-operation": {
                    "post": {
                        EXPORT_INTEGRATION: {
                            "type": "http_proxy",
                            "httpMethod": "POST",
                            "connectionType": "VPC_LINK",
                            "connectionId": "link",
                            "integrationTarget": TARGET,
                            "uri": "https://identity.generated.example:443/generated-target",
                        }
                    }
                }
            }
        },
    }


def test_reviewed_gateway_binding_covers_all_five_operations(binding, readback) -> None:
    routes = {route: "/dev" + route for route in APPROVED_API_PATHS}
    binding = replace(binding, routes=routes)
    endpoint_policy = json.loads(readback["endpoint"]["PolicyDocument"])
    endpoint_policy["Statement"][0]["Resource"] = [f"{API}/dev/POST{route}" for route in routes]
    readback["endpoint"]["PolicyDocument"] = json.dumps(endpoint_policy)
    readback["deployment"]["apiSummary"] = {
        route: {"POST": {"authorizationType": "AWS_IAM", "apiKeyRequired": False}}
        for route in routes
    }
    integration = readback["stage_export"]["paths"]["/generated-operation"]["post"][
        EXPORT_INTEGRATION
    ]
    readback["stage_export"]["paths"] = {
        route: {
            "post": {
                EXPORT_INTEGRATION: {
                    **integration,
                    "uri": f"https://identity.generated.example:443{backend_path}",
                }
            }
        }
        for route, backend_path in routes.items()
    }
    check_private_gateway(binding, **readback)
    readback["stage_export"]["paths"].pop("/internal/browser-identity/v1/sessions/end")
    with pytest.raises(PrivateGatewayPolicyError, match="PRIVATE_GATEWAY_METHOD_MISMATCH"):
        check_private_gateway(binding, **readback)


@pytest.mark.parametrize(
    "fields",
    [("integrationTarget",), ("integration-target",), ("integrationTarget", "integration-target")],
)
def test_exact_dev_binding_passes_source_check(binding, readback, fields):
    integration = readback["stage_export"]["paths"]["/generated-operation"]["post"][
        EXPORT_INTEGRATION
    ]
    target = integration.pop("integrationTarget")
    integration.update(dict.fromkeys(fields, target))
    check_private_gateway(binding, **readback)


@pytest.mark.parametrize(
    ("resource", "field", "wrong"),
    [
        ("rest_api", "id", "different-api"),
        ("rest_api", "endpointConfiguration", {"types": ["REGIONAL"]}),
        ("endpoint", "VpcEndpointId", "vpce-4321"),
        ("endpoint", "VpcId", "vpc-4321"),
        ("endpoint", "OwnerId", "444455556666"),
        ("endpoint", "ServiceName", "com.amazonaws.us-east-1.execute-api"),
        ("endpoint", "PrivateDnsEnabled", True),
        ("stage", "stageName", "prod"),
        ("stage", "deploymentId", "other-deployment"),
        ("stage", "clientCertificateId", "foreign-certificate"),
        ("stage", "cacheClusterEnabled", True),
        ("stage", "tracingEnabled", True),
        ("stage", "canarySettings", {"percentTraffic": 1}),
        ("stage", "accessLogSettings", {"format": "$context.identity.user"}),
        ("deployment", "id", "other-deployment"),
        ("deployment", "apiSummary", {}),
    ],
)
def test_refuses_foreign_or_unsafe_provider_configuration(
    binding, readback, resource, field, wrong
):
    readback[resource][field] = wrong
    with pytest.raises(PrivateGatewayPolicyError):
        check_private_gateway(binding, **readback)


@pytest.mark.parametrize("field", ["dataTraceEnabled", "cachingEnabled", "metricsEnabled"])
def test_refuses_unsafe_per_method_logging_or_caching(binding, readback, field):
    readback["stage"]["methodSettings"]["/generated-operation/POST"] = {field: True}
    with pytest.raises(PrivateGatewayPolicyError, match="PRIVATE_GATEWAY_STAGE_UNSAFE"):
        check_private_gateway(binding, **readback)


def test_refuses_api_resource_allow_or_collapsed_deny_conditions(binding, readback):
    policy = json.loads(readback["rest_api"]["policy"])
    first, second = policy["Statement"]
    first["Condition"].update(second["Condition"])
    policy["Statement"] = [first]
    readback["rest_api"]["policy"] = json.dumps(policy)
    with pytest.raises(PrivateGatewayPolicyError, match="PRIVATE_GATEWAY_API_POLICY_MISMATCH"):
        check_private_gateway(binding, **readback)
    policy["Statement"] = [first, second, {**second, "Effect": "Allow"}]
    readback["rest_api"]["policy"] = json.dumps(policy)
    with pytest.raises(PrivateGatewayPolicyError, match="PRIVATE_GATEWAY_API_POLICY_MISMATCH"):
        check_private_gateway(binding, **readback)


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("Principal", "*"),
        ("Principal", {"AWS": "arn:aws:iam::111122223333:role/other"}),
        ("Resource", [f"{API}/dev/*"]),
        ("Resource", [f"{API}/dev/POST/unapproved"]),
    ],
)
def test_refuses_endpoint_policy_role_or_path_broadening(binding, readback, field, wrong):
    policy = json.loads(readback["endpoint"]["PolicyDocument"])
    policy["Statement"][0][field] = wrong
    readback["endpoint"]["PolicyDocument"] = json.dumps(policy)
    with pytest.raises(PrivateGatewayPolicyError, match="PRIVATE_GATEWAY_ENDPOINT_POLICY_MISMATCH"):
        check_private_gateway(binding, **readback)


def test_refuses_unapproved_method_in_deployment_snapshot(binding, readback):
    method = readback["deployment"]["apiSummary"]["/generated-operation"]["POST"]
    method["authorizationType"] = "NONE"
    with pytest.raises(PrivateGatewayPolicyError, match="PRIVATE_GATEWAY_METHOD_MISMATCH"):
        check_private_gateway(binding, **readback)


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("connectionId", "foreign-link"),
        ("integrationTarget", TARGET + "0"),
        ("uri", "https://identity.generated.example:443/foreign"),
        ("uri", "http://identity.generated.example:443/generated-target"),
        ("uri", "https://identity.generated.example:443/generated-target?secret=sentinel"),
        ("tlsConfig", {"insecureSkipVerification": True}),
        ("requestParameters", {"integration.request.header.Host": "'other.generated.example'"}),
    ],
)
def test_refuses_deployed_target_drift_or_unverified_tls(binding, readback, field, wrong):
    operation = readback["stage_export"]["paths"]["/generated-operation"]["post"]
    operation[EXPORT_INTEGRATION][field] = wrong
    with pytest.raises(PrivateGatewayPolicyError, match="PRIVATE_GATEWAY_TARGET_MISMATCH"):
        check_private_gateway(binding, **readback)


@pytest.mark.parametrize("path", ["/../private", "/{proxy+}", "/a%2Fb", "/path?token=sentinel"])
def test_refuses_ambiguous_approved_paths(binding, readback, path):
    with pytest.raises(PrivateGatewayPolicyError, match="PRIVATE_GATEWAY_BINDING_INVALID"):
        check_private_gateway(replace(binding, routes={path: "/generated-target"}), **readback)


def test_refuses_duplicate_policy_members(binding, readback):
    policy = readback["endpoint"]["PolicyDocument"]
    readback["endpoint"]["PolicyDocument"] = policy.replace(
        '"Effect": "Allow"', '"Effect": "Deny", "Effect": "Allow"'
    )
    with pytest.raises(PrivateGatewayPolicyError, match="PRIVATE_GATEWAY_POLICY_INVALID"):
        check_private_gateway(binding, **readback)


def test_refuses_missing_integration_target(binding, readback):
    operation = readback["stage_export"]["paths"]["/generated-operation"]["post"]
    del operation[EXPORT_INTEGRATION]["integrationTarget"]
    with pytest.raises(PrivateGatewayPolicyError, match="PRIVATE_GATEWAY_TARGET_MISMATCH"):
        check_private_gateway(binding, **readback)


@pytest.mark.parametrize("field", ["integrationTarget", "integration-target"])
@pytest.mark.parametrize("wrong", [TARGET + "0", None])
def test_refuses_conflicting_integration_target_keys(binding, readback, field, wrong):
    integration = readback["stage_export"]["paths"]["/generated-operation"]["post"][
        EXPORT_INTEGRATION
    ]
    integration["integration-target"] = TARGET
    integration[field] = wrong
    with pytest.raises(PrivateGatewayPolicyError, match="PRIVATE_GATEWAY_TARGET_MISMATCH"):
        check_private_gateway(binding, **readback)
