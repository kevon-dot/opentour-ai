"""OPE-213 offline preflight over an explicitly approved resource inventory.

The inventory is supplied by the deployment authority, separately from the requested
manifest. Neither a caller-provided ARN nor a self-claimed account is authority.
This module performs no provider calls and does not authenticate an AWS inventory.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ENVIRONMENTS = frozenset({"dev", "test", "prod"})
SINGLES = {
    "vpc_ref": "vpc",
    "ingress_ref": "ingress",
    "database_ref": "database",
    "audit_sink_ref": "audit_sink",
    "network_policy_ref": "network_policy",
}
LISTS = {
    "subnet_refs": "subnet",
    "service_refs": "service",
    "queue_refs": "queue",
    "registry_refs": "registry",
    "secrets_refs": "secret",
    "key_refs": "key",
}
FIELDS = {"environment", "account_id", "region", "configuration_digest", *SINGLES, *LISTS}
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
ACCOUNT = re.compile(r"[0-9]{12}\Z")
REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+\Z")
SERVICES = {
    "vpc": {"ec2"},
    "subnet": {"ec2"},
    "network_policy": {"ec2"},
    "ingress": {"elasticloadbalancing"},
    "database": {"rds"},
    "service": {"ecs"},
    "queue": {"sqs"},
    "registry": {"ecr"},
    "secret": {"secretsmanager"},
    "key": {"kms"},
    "audit_sink": {"execute-api", "elasticloadbalancing"},
}
RESOURCE_PREFIXES = {
    "vpc": "vpc/",
    "subnet": "subnet/",
    "network_policy": "security-group/",
    "ingress": "loadbalancer/app/",
    "database": "db:",
    "service": "service/",
    "registry": "repository/",
    "secret": "secret:",
    "key": "key/",
}


class EnvironmentError(ValueError):
    """Finite reason code; never embeds an untrusted resource or secret value."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def canonical_digest(value: Any) -> str:
    try:
        raw = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    except (TypeError, ValueError) as exc:
        raise EnvironmentError("INVALID_CANONICAL_CONFIGURATION") from exc
    return hashlib.sha256(raw).hexdigest()


def _text(value: Any, pattern: re.Pattern[str] | None = None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or value != value.strip()
        or (pattern is not None and not pattern.fullmatch(value))
    ):
        raise EnvironmentError("INVALID_FIELD")
    return value


def _integer(value: Any, minimum: int = 1) -> int:
    if type(value) is not int or not minimum <= value <= 2_147_483_647:
        raise EnvironmentError("INVALID_INTEGER")
    return value


@dataclass(frozen=True)
class ResourceBinding:
    """An independently approved observed/planned reference, never request authority."""

    arn: str
    kind: str
    environment: str
    account_id: str
    region: str
    configuration_digest: str
    provider_resource_id: str | None = None


@dataclass(frozen=True)
class EnvironmentPolicy:
    production_account: str
    nonproduction_account: str
    audit_account: str
    region: str
    partition: str = "aws"
    approved_audit_arns: frozenset[str] = frozenset()

    def validate(self) -> None:
        accounts = (self.production_account, self.nonproduction_account, self.audit_account)
        for account in accounts:
            _text(account, ACCOUNT)
        if len(set(accounts)) != 3:
            raise EnvironmentError("ACCOUNTS_NOT_ISOLATED")
        _text(self.region, REGION)
        if self.partition not in {"aws", "aws-us-gov", "aws-cn"}:
            raise EnvironmentError("UNSUPPORTED_PARTITION")
        if ((self.partition == "aws-cn") != self.region.startswith("cn-")) or (
            (self.partition == "aws-us-gov") != self.region.startswith("us-gov-")
        ):
            raise EnvironmentError("PARTITION_REGION_MISMATCH")

    def account_for(self, environment: str) -> str:
        if environment not in ENVIRONMENTS:
            raise EnvironmentError("UNKNOWN_ENVIRONMENT")
        return self.production_account if environment == "prod" else self.nonproduction_account


@dataclass(frozen=True)
class VerifiedEnvironment:
    environment: str
    account_id: str
    region: str
    configuration_digest: str
    reference_digests: tuple[tuple[str, str], ...]


def validate_manifest(
    value: Any, policy: EnvironmentPolicy, inventory: Mapping[str, ResourceBinding]
) -> VerifiedEnvironment:
    """Fail before provisioning; request references must match approved immutable bindings."""
    policy.validate()
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise EnvironmentError("CLOSED_MANIFEST_REQUIRED")
    environment = _text(value["environment"])
    account = _text(value["account_id"], ACCOUNT)
    region = _text(value["region"], REGION)
    if account != policy.account_for(environment) or region != policy.region:
        raise EnvironmentError("ENVIRONMENT_SCOPE_MISMATCH")
    digest = _text(value["configuration_digest"], SHA256)
    expected_digest = canonical_digest(
        {k: v for k, v in value.items() if k != "configuration_digest"}
    )
    if digest != expected_digest:
        raise EnvironmentError("CONFIGURATION_DIGEST_MISMATCH")
    references: list[tuple[str, str]] = []
    seen: set[str] = set()
    for field, kind in {**SINGLES, **LISTS}.items():
        values = value[field] if field in LISTS else [value[field]]
        if not isinstance(values, list) or not 1 <= len(values) <= 128:
            raise EnvironmentError("INVALID_REFERENCE_INVENTORY")
        for reference in values:
            if not isinstance(reference, dict) or set(reference) != {"arn", "configuration_digest"}:
                raise EnvironmentError("CLOSED_REFERENCE_REQUIRED")
            arn = _text(reference["arn"])
            reference_digest = _text(reference["configuration_digest"], SHA256)
            if arn in seen:
                raise EnvironmentError("DUPLICATE_REFERENCE")
            seen.add(arn)
            binding = inventory.get(arn)
            if (
                not isinstance(binding, ResourceBinding)
                or binding.arn != arn
                or binding.kind != kind
            ):
                raise EnvironmentError("UNAPPROVED_REFERENCE")
            if reference_digest != binding.configuration_digest:
                raise EnvironmentError("STALE_REFERENCE")
            parts = arn.split(":", 5)
            if (
                len(parts) != 6
                or parts[0] != "arn"
                or parts[1] != policy.partition
                or parts[2] not in SERVICES[kind]
                or not parts[5]
                or any(c in arn for c in ("*", "?", "${", "{{"))
            ):
                raise EnvironmentError("INVALID_RESOURCE_ARN")
            prefix = RESOURCE_PREFIXES.get(kind)
            if prefix is not None and (
                not parts[5].startswith(prefix) or len(parts[5]) == len(prefix)
            ):
                raise EnvironmentError("RESOURCE_KIND_MISMATCH")
            if kind == "queue" and re.fullmatch(r"[A-Za-z0-9_-]+(?:\.fifo)?", parts[5]) is None:
                raise EnvironmentError("RESOURCE_KIND_MISMATCH")
            if (
                kind == "audit_sink"
                and parts[2] == "elasticloadbalancing"
                and not parts[5].startswith("loadbalancer/app/")
            ):
                raise EnvironmentError("RESOURCE_KIND_MISMATCH")
            # This first profile uses regional resources with account-bearing ARNs.
            # Accountless/global resources require a future explicit owner-qualified profile.
            if parts[3] != binding.region or parts[4] != binding.account_id:
                raise EnvironmentError("ARN_INVENTORY_MISMATCH")
            if field == "audit_sink_ref":
                if (
                    arn not in policy.approved_audit_arns
                    or binding.account_id != policy.audit_account
                    or binding.region != policy.region
                    or binding.environment != "audit"
                ):
                    raise EnvironmentError("UNAPPROVED_AUDIT_DESTINATION")
            elif (
                binding.account_id != account
                or binding.region != region
                or binding.environment != environment
            ):
                raise EnvironmentError("CROSS_ENVIRONMENT_REFERENCE")
            references.append((arn, reference_digest))
    if len(value["subnet_refs"]) < 2 or len(value["service_refs"]) < 3:
        raise EnvironmentError("INCOMPLETE_CONTROL_TOPOLOGY")
    return VerifiedEnvironment(environment, account, region, digest, tuple(sorted(references)))


def connection_ceiling(
    database_max: int,
    migration_reserve: int,
    recovery_reserve: int,
    other_connections: int,
    *,
    pool_per_api_instance: int = 20,
    requested_max_instances: int,
) -> int:
    """Calculate a hard aggregate budget, including non-API consumers and recovery."""
    for n in (
        database_max,
        migration_reserve,
        recovery_reserve,
        pool_per_api_instance,
        requested_max_instances,
    ):
        _integer(n)
    _integer(other_connections, 0)
    available = database_max - migration_reserve - recovery_reserve - other_connections
    ceiling = available // pool_per_api_instance
    if ceiling < 1 or requested_max_instances > ceiling:
        raise EnvironmentError("DATABASE_CONNECTION_BUDGET_EXCEEDED")
    return ceiling


def security_configuration_diff(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    """Reviewable structural diff; never interpret template comments as approval."""
    changes: list[dict[str, Any]] = []
    old = before.get("Resources", {})
    new = after.get("Resources", {})
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise EnvironmentError("INVALID_TEMPLATE_RESOURCES")
    for resource_id in sorted(set(old) | set(new)):
        a, b = old.get(resource_id), new.get(resource_id)
        if a == b:
            continue
        current = b if b is not None else a
        if not isinstance(current, dict) or not isinstance(current.get("Type"), str):
            raise EnvironmentError("INVALID_TEMPLATE_RESOURCE")
        resource_type = current["Type"]
        security_properties = {
            "AssumeRolePolicyDocument",
            "Policies",
            "PermissionsBoundary",
            "PolicyDocument",
            "Policy",
            "KeyPolicy",
            "TaskRoleArn",
            "ExecutionRoleArn",
            "EnableExecuteCommand",
            "PubliclyAccessible",
            "VPCSecurityGroups",
            "SecurityGroupIds",
            "SecurityGroups",
            "SecurityGroupIngress",
            "SecurityGroupEgress",
            "NetworkConfiguration",
            "SubnetIds",
            "VpcId",
            "CidrBlock",
            "IpProtocol",
            "FromPort",
            "ToPort",
            "CidrIp",
            "CidrIpv6",
            "GroupId",
            "DestinationSecurityGroupId",
            "SourceSecurityGroupId",
            "DestinationPrefixListId",
            "DestinationCidrBlock",
            "GatewayId",
            "NatGatewayId",
            "VpcEndpointType",
            "ServiceName",
            "Principal",
            "Action",
            "Resource",
            "KmsKeyId",
            "StorageEncrypted",
            "EnableIAMDatabaseAuthentication",
            "DBSubnetGroupName",
            "RoleARN",
        }
        old_properties = a.get("Properties", {}) if isinstance(a, dict) else {}
        new_properties = b.get("Properties", {}) if isinstance(b, dict) else {}
        if not isinstance(old_properties, dict) or not isinstance(new_properties, dict):
            raise EnvironmentError("INVALID_TEMPLATE_RESOURCE")
        security_changes = [
            {"property": key, "before": old_properties.get(key), "after": new_properties.get(key)}
            for key in sorted(security_properties & (set(old_properties) | set(new_properties)))
            if old_properties.get(key) != new_properties.get(key)
        ]
        security = (
            bool(security_changes)
            or resource_type.startswith(("AWS::IAM::", "AWS::EC2::", "AWS::KMS::"))
            or resource_type.endswith("Policy")
        )
        changes.append(
            {
                "logical_id": resource_id,
                "resource_type": resource_type,
                "change": "add" if a is None else "remove" if b is None else "modify",
                "before_sha256": canonical_digest(a) if a is not None else None,
                "after_sha256": canonical_digest(b) if b is not None else None,
                "security_review_required": security,
                "security_changes": security_changes,
            }
        )
    # Parameter/condition/mapping changes can alter every resolved permission. Do not
    # publish defaults (which may be secrets), or pretend a local diff resolves CFN.
    configuration_changes = [
        {
            "section": key,
            "before_sha256": canonical_digest(before.get(key)),
            "after_sha256": canonical_digest(after.get(key)),
        }
        for key in ("Parameters", "Conditions", "Mappings", "Rules", "Transform")
        if before.get(key) != after.get(key)
    ]
    return {
        "changes": changes,
        "configuration_changes": configuration_changes,
        "security_review_required": bool(configuration_changes)
        or any(c["security_review_required"] for c in changes),
        "provider_change_set": "NOT_RUN",
    }


BASELINE_COST_COMPONENTS = frozenset(
    {
        "alb",
        "rds",
        "nat",
        "vpc_endpoints",
        "logs",
        "audit_storage",
        "fargate",
        "sqs",
        "ecr",
        "kms",
        "secrets",
        "backup_storage",
        "data_transfer",
    }
)


def validate_cost_inventory(value: Any) -> None:
    """Require quantities/units and explicit zero explanations; never invent prices."""
    if not isinstance(value, dict) or set(value) != BASELINE_COST_COMPONENTS:
        raise EnvironmentError("INCOMPLETE_BASELINE_COST_INVENTORY")
    for item in value.values():
        if not isinstance(item, dict) or set(item) != {"quantity", "unit", "zero_reason"}:
            raise EnvironmentError("INVALID_COST_COMPONENT")
        _integer(item["quantity"], 0)
        _text(item["unit"])
        if item["quantity"] == 0:
            _text(item["zero_reason"])
        elif item["zero_reason"] is not None:
            raise EnvironmentError("UNEXPECTED_ZERO_REASON")


@dataclass(frozen=True)
class PreparedStack:
    """Exact reviewed bytes/parameters ready for a separate authorized provider operator."""

    template_sha256: str
    parameters_json: str
    configuration_digest: str
    environment: str
    account_id: str
    region: str
    provider_change_set: str = "NOT_RUN"


def prepare_stack(
    name: str,
    supplied: Any,
    policy: EnvironmentPolicy,
    inventory: Mapping[str, ResourceBinding],
    *,
    approved_template_sha256: str,
    approved_images: frozenset[str] = frozenset(),
    approved_ecr_s3_prefix_lists: frozenset[str] = frozenset(),
) -> PreparedStack:
    """Resolve only these owned templates; reject unsafe input before any cloud call.

    Approval inputs come from the independent deployment/release authority, not the
    submitted request. A successful result is offline conformance, not AWS authority.
    """
    from pathlib import Path

    policy.validate()
    if policy.partition != "aws":
        raise EnvironmentError("UNQUALIFIED_TEMPLATE_PARTITION")
    if name not in {"control-plane", "environment-boundaries"}:
        raise EnvironmentError("UNKNOWN_STACK")
    raw = (Path(__file__).parent / "cloudformation" / (name + ".yaml")).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != _text(approved_template_sha256, SHA256):
        raise EnvironmentError("UNREVIEWED_TEMPLATE")
    template = json.loads(raw)
    definitions = template["Parameters"]
    if not isinstance(supplied, dict) or set(supplied) - set(definitions):
        raise EnvironmentError("UNKNOWN_STACK_PARAMETER")
    parameters: dict[str, Any] = {}
    for key, definition in definitions.items():
        if key not in supplied and "Default" not in definition:
            raise EnvironmentError("MISSING_STACK_PARAMETER")
        value = supplied.get(key, definition.get("Default"))
        if definition["Type"] == "Number":
            _integer(value, definition.get("MinValue", 0))
            if value > definition.get("MaxValue", 2_147_483_647):
                raise EnvironmentError("STACK_PARAMETER_OUT_OF_RANGE")
        elif definition["Type"] == "CommaDelimitedList":
            if not isinstance(value, list) or not 1 <= len(value) <= 128:
                raise EnvironmentError("INVALID_STACK_PARAMETER_LIST")
            if len({_text(item) for item in value}) != len(value):
                raise EnvironmentError("DUPLICATE_REFERENCE")
        elif value != "" or definition.get("Default") != "":
            _text(value)
        if "AllowedValues" in definition and value not in definition["AllowedValues"]:
            raise EnvironmentError("STACK_PARAMETER_OUT_OF_RANGE")
        if "AllowedPattern" in definition and (
            not isinstance(value, str) or re.fullmatch(definition["AllowedPattern"], value) is None
        ):
            raise EnvironmentError("STACK_PARAMETER_OUT_OF_RANGE")
        parameters[key] = value
    environment = parameters["Environment"]
    account = policy.account_for(environment)
    if parameters["ExpectedAccountId"] != account:
        raise EnvironmentError("ENVIRONMENT_SCOPE_MISMATCH")

    def approved_reference(
        arn: str, kind: str, service: str, prefix: str, *, global_arn: bool = False
    ) -> None:
        binding = inventory.get(arn)
        parts = arn.split(":", 5)
        if (
            binding is None
            or binding.arn != arn
            or binding.kind != kind
            or not SHA256.fullmatch(binding.configuration_digest)
        ):
            raise EnvironmentError("UNAPPROVED_REFERENCE")
        if (
            len(parts) != 6
            or parts[:3] != ["arn", policy.partition, service]
            or parts[3] != ("" if global_arn else policy.region)
            or parts[4] != account
            or not parts[5].startswith(prefix)
            or len(parts[5]) == len(prefix)
            or any(char in arn for char in ("*", "?", "${", "{{"))
        ):
            raise EnvironmentError("RESOURCE_KIND_MISMATCH")
        if (
            binding.account_id != account
            or binding.region != parts[3]
            or binding.environment
            not in ({environment, "shared_oidc"} if kind == "oidc" else {environment})
        ):
            raise EnvironmentError("CROSS_ENVIRONMENT_REFERENCE")

    if name == "control-plane":
        import ipaddress

        try:
            network = ipaddress.ip_network(parameters["VpcCidr"], strict=True)
        except ValueError as exc:
            raise EnvironmentError("INVALID_VPC_CIDR") from exc
        if network.version != 4 or not network.is_private or not 16 <= network.prefixlen <= 21:
            raise EnvironmentError("INVALID_VPC_CIDR")
        approved_reference(parameters["CertificateArn"], "certificate", "acm", "certificate/")
        if parameters["EcrS3PrefixListId"] not in approved_ecr_s3_prefix_lists:
            raise EnvironmentError("UNAPPROVED_ECR_S3_PREFIX_LIST")
        connection_ceiling(
            parameters["DatabaseMaxConnections"],
            parameters["MigrationConnectionReserve"],
            parameters["RecoveryConnectionReserve"],
            parameters["OtherConnectionBudget"],
            pool_per_api_instance=parameters["ApiPoolPerInstance"],
            requested_max_instances=parameters["ApiMaxInstances"],
        )
        if (
            parameters["DatabaseEngineFamily"]
            != "postgres" + parameters["DatabaseEngineVersion"].split(".")[0]
        ):
            raise EnvironmentError("DATABASE_ENGINE_FAMILY_MISMATCH")
        for role in (
            "BffTaskRoleArn",
            "ApiTaskRoleArn",
            "WorkerTaskRoleArn",
            "TaskExecutionRoleArn",
        ):
            if parameters[role]:
                approved_reference(parameters[role], "role", "iam", "role/", global_arn=True)
            elif parameters["ApplicationEnabled"] == "true":
                raise EnvironmentError("APPLICATION_ROLE_REQUIRED")
        for image in ("BffImageDigest", "ApiImageDigest", "WorkerImageDigest"):
            value = parameters[image]
            if not value and parameters["ApplicationEnabled"] == "false":
                continue
            suffix = "amazonaws.com.cn" if policy.partition == "aws-cn" else "amazonaws.com"
            if (
                not value.startswith(f"{account}.dkr.ecr.{policy.region}.{suffix}/")
                or value not in approved_images
            ):
                raise EnvironmentError("UNQUALIFIED_APPLICATION_IMAGE")
    else:
        for parameter, kind, service, prefix in (
            ("ApplicationSecretArn", "secret", "secretsmanager", "secret:"),
            ("DataKeyArn", "key", "kms", "key/"),
            ("WakeupQueueArn", "queue", "sqs", ""),
        ):
            approved_reference(parameters[parameter], kind, service, prefix)
        for parameter, kind, service, prefix in (
            ("RepositoryArns", "registry", "ecr", "repository/"),
            ("DeclaredServiceArns", "service", "ecs", "service/"),
        ):
            if len(parameters[parameter]) != 3:
                raise EnvironmentError("INCOMPLETE_CONTROL_TOPOLOGY")
            for arn in parameters[parameter]:
                approved_reference(arn, kind, service, prefix)
        for parameter in ("BffLogGroupArn", "ApiLogGroupArn", "WorkerLogGroupArn"):
            approved_reference(parameters[parameter], "log", "logs", "log-group:")
        databases = [
            binding
            for binding in inventory.values()
            if binding.kind == "database"
            and binding.provider_resource_id == parameters["DatabaseResourceId"]
        ]
        if len(databases) != 1:
            raise EnvironmentError("UNAPPROVED_DATABASE_RESOURCE_ID")
        approved_reference(databases[0].arn, "database", "rds", "db:")
        approved_reference(
            parameters["OidcProviderArn"], "oidc", "iam", "oidc-provider/", global_arn=True
        )
    canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return PreparedStack(
        digest,
        canonical,
        canonical_digest({"template": digest, "parameters": parameters}),
        environment,
        account,
        policy.region,
    )
