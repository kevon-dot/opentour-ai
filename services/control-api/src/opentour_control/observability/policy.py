"""Offline OPE-214 policy check for the dedicated dev identity gateway."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

_SAFE_PATH = r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*"
_INTEGRATION = "x-amazon-apigateway-integration"
# The AWS extension reference and private integration example use different keys.
_INTEGRATION_TARGET_FIELDS = {"integrationTarget", "integration-target"}


class PrivateGatewayPolicyError(ValueError):
    """A candidate or observed gateway fails the approved private profile."""


@dataclass(frozen=True)
class PrivateGatewayBinding:
    """Values from an independently verified dev deployment inventory."""

    account_id: str
    region: str
    api_id: str
    deployment_id: str
    endpoint_id: str
    vpc_id: str
    bff_role_arn: str
    link_id: str
    target_arn: str
    backend_host: str
    client_certificate_id: str
    routes: Mapping[str, str]  # Approved API resource path to exact backend URI path.


def _require(condition: object, code: str) -> None:
    if not condition:
        raise PrivateGatewayPolicyError(code)


def _fields(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PrivateGatewayPolicyError("PRIVATE_GATEWAY_READBACK_INVALID")
    return value


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields = dict(pairs)
    _require(len(fields) == len(pairs), "PRIVATE_GATEWAY_POLICY_INVALID")
    return fields


def _policy_statements(value: object) -> list[dict[str, object]]:
    if isinstance(value, str):
        try:
            value = json.loads(value, object_pairs_hook=_unique_fields)
        except (ValueError, RecursionError):
            raise PrivateGatewayPolicyError("PRIVATE_GATEWAY_POLICY_INVALID") from None
    fields = _fields(value)
    _require(
        set(fields) <= {"Version", "Statement", "Id"} and fields.get("Version") == "2012-10-17",
        "PRIVATE_GATEWAY_POLICY_INVALID",
    )
    statements = fields.get("Statement")
    if not isinstance(statements, list):
        raise PrivateGatewayPolicyError("PRIVATE_GATEWAY_POLICY_INVALID")
    # AWS policy labels do not affect authorization.
    return [
        {name: item for name, item in _fields(statement).items() if name != "Sid"}
        for statement in statements
    ]


def check_private_gateway(
    approved: PrivateGatewayBinding,
    *,
    rest_api: Mapping[str, object],
    endpoint: Mapping[str, object],
    stage: Mapping[str, object],
    deployment: Mapping[str, object],
    stage_export: Mapping[str, object],
) -> None:
    """Check provider configuration without obtaining it or admitting traffic.

    The caller supplies trusted GetRestApi, DescribeVpcEndpoints, GetStage,
    GetDeployment(embed=apisummary), and stage GetExport(integrations) readbacks.
    The resource policy is the current API configuration. Deployments are snapshots,
    so effective authorization still needs provider evidence. This check does not
    prove task-role boundaries, network isolation, or certificate presentation.
    """
    role_prefix = f"arn:aws:iam::{approved.account_id}:role/"
    target_prefix = f"arn:aws:elasticloadbalancing:{approved.region}:{approved.account_id}:"
    approved_routes = dict(approved.routes)
    _require(
        re.fullmatch(r"[0-9]{12}", approved.account_id)
        and re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", approved.region)
        and re.fullmatch(r"[a-z0-9]+", approved.api_id)
        and re.fullmatch(r"vpce-[0-9a-f]+", approved.endpoint_id)
        and re.fullmatch(r"vpc-[0-9a-f]+", approved.vpc_id)
        and re.fullmatch(re.escape(role_prefix) + r"[A-Za-z0-9+=,.@_/-]+", approved.bff_role_arn)
        and re.fullmatch(
            re.escape(target_prefix)
            + r"(?:loadbalancer/net/[A-Za-z0-9-]+/[0-9a-f]+"
            + r"|listener/net/[A-Za-z0-9-]+/[0-9a-f]+/[0-9a-f]+)",
            approved.target_arn,
        )
        and re.fullmatch(
            r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*",
            approved.backend_host,
        )
        and approved.deployment_id
        and approved.link_id
        and approved.client_certificate_id
        and approved_routes
        and all(re.fullmatch(_SAFE_PATH, path) for path in approved_routes)
        and all(re.fullmatch(_SAFE_PATH, path) for path in approved_routes.values())
        and len(set(approved_routes.values())) == len(approved_routes),
        "PRIVATE_GATEWAY_BINDING_INVALID",
    )
    endpoint_config = _fields(rest_api.get("endpointConfiguration"))
    _require(
        rest_api.get("id") == approved.api_id
        and endpoint_config.get("types") == ["PRIVATE"]
        and endpoint_config.get("vpcEndpointIds") == [approved.endpoint_id]
        and endpoint.get("VpcEndpointId") == approved.endpoint_id
        and endpoint.get("OwnerId") == approved.account_id
        and endpoint.get("VpcId") == approved.vpc_id
        and endpoint.get("VpcEndpointType") == "Interface"
        and endpoint.get("ServiceName") == f"com.amazonaws.{approved.region}.execute-api"
        and endpoint.get("PrivateDnsEnabled") is False,
        "PRIVATE_GATEWAY_ENDPOINT_MISMATCH",
    )
    api_arn = f"arn:aws:execute-api:{approved.region}:{approved.account_id}:{approved.api_id}"
    endpoint_statements = _policy_statements(endpoint.get("PolicyDocument"))
    _require(
        len(endpoint_statements) == 1
        and endpoint_statements[0].get("Effect") == "Allow"
        and endpoint_statements[0].get("Action") == "execute-api:Invoke"
        and endpoint_statements[0].get("Principal") == {"AWS": approved.bff_role_arn}
        and set(endpoint_statements[0]) == {"Action", "Effect", "Principal", "Resource"},
        "PRIVATE_GATEWAY_ENDPOINT_POLICY_MISMATCH",
    )
    resources = endpoint_statements[0]["Resource"]
    _require(
        isinstance(resources, list)
        and sorted(f"{api_arn}/dev/POST{path}" for path in approved_routes)
        == sorted(item for item in resources if isinstance(item, str))
        and len(resources) == len(approved_routes),
        "PRIVATE_GATEWAY_ENDPOINT_POLICY_MISMATCH",
    )
    denies = _policy_statements(rest_api.get("policy"))
    expected_denies = [
        {
            "Effect": "Deny",
            "Action": "execute-api:Invoke",
            "Principal": "*",
            "Resource": f"{api_arn}/*",
            "Condition": {operator: {key: value}},
        }
        for operator, key, value in (
            ("StringNotEquals", "aws:SourceVpce", approved.endpoint_id),
            ("ArnNotEquals", "aws:PrincipalArn", approved.bff_role_arn),
        )
    ]
    _require(
        len(denies) == len(expected_denies) and all(deny in denies for deny in expected_denies),
        "PRIVATE_GATEWAY_API_POLICY_MISMATCH",
    )
    _require(
        stage.get("stageName") == "dev"
        and stage.get("deploymentId") == approved.deployment_id
        and deployment.get("id") == approved.deployment_id
        and stage.get("clientCertificateId") == approved.client_certificate_id
        and stage.get("cacheClusterEnabled", False) is False
        and stage.get("tracingEnabled", False) is False
        and not any(
            stage.get(field) for field in ("accessLogSettings", "canarySettings", "variables")
        ),
        "PRIVATE_GATEWAY_STAGE_UNSAFE",
    )
    for setting in _fields(stage.get("methodSettings", {})).values():
        method = _fields(setting)
        _require(
            method.get("loggingLevel", "OFF") == "OFF"
            and all(
                method.get(field, False) is False
                for field in ("dataTraceEnabled", "cachingEnabled", "metricsEnabled")
            ),
            "PRIVATE_GATEWAY_STAGE_UNSAFE",
        )
    summary = _fields(deployment.get("apiSummary"))
    export_paths = _fields(stage_export.get("paths"))
    _require(
        set(summary) == set(export_paths) == set(approved_routes),
        "PRIVATE_GATEWAY_METHOD_MISMATCH",
    )
    for route, backend_path in approved_routes.items():
        _require(
            summary[route] == {"POST": {"authorizationType": "AWS_IAM", "apiKeyRequired": False}},
            "PRIVATE_GATEWAY_METHOD_MISMATCH",
        )
        operations = _fields(export_paths[route])
        _require(set(operations) == {"post"}, "PRIVATE_GATEWAY_METHOD_MISMATCH")
        integration = _fields(_fields(operations["post"]).get(_INTEGRATION))
        target_fields = _INTEGRATION_TARGET_FIELDS & integration.keys()
        _require(
            integration.get("type") == "http_proxy"
            and integration.get("httpMethod") == "POST"
            and integration.get("connectionType") == "VPC_LINK"
            and integration.get("connectionId") == approved.link_id
            and target_fields
            and all(integration[key] == approved.target_arn for key in target_fields)
            and integration.get("uri") == f"https://{approved.backend_host}:443{backend_path}"
            and not any(
                integration.get(field)
                for field in ("requestParameters", "requestTemplates", "credentials")
            )
            and _fields(integration.get("tlsConfig", {})).get("insecureSkipVerification", False)
            is False,
            "PRIVATE_GATEWAY_TARGET_MISMATCH",
        )
