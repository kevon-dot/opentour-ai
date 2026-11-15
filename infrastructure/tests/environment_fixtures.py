"""Generated immutable sentinels; no account discovery or provider authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from infrastructure.environment_contract import (
    LISTS,
    SINGLES,
    EnvironmentPolicy,
    ResourceBinding,
    canonical_digest,
)

PROD, NONPROD, AUDIT = "111111111111", "222222222222", "333333333333"
REGION = "us-east-1"
TEMPLATES = Path(__file__).parents[1] / "cloudformation"


def template(name: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((TEMPLATES / (name + ".yaml")).read_bytes())
    return result


def template_digest(name: str) -> str:
    return hashlib.sha256((TEMPLATES / (name + ".yaml")).read_bytes()).hexdigest()


def fixture(
    environment: str = "dev",
) -> tuple[dict[str, Any], EnvironmentPolicy, dict[str, ResourceBinding]]:
    account = PROD if environment == "prod" else NONPROD
    inventory: dict[str, ResourceBinding] = {}
    resources = {
        "vpc": ("ec2", "vpc/"),
        "subnet": ("ec2", "subnet/"),
        "network_policy": ("ec2", "security-group/"),
        "ingress": ("elasticloadbalancing", "loadbalancer/app/"),
        "database": ("rds", "db:"),
        "service": ("ecs", "service/cluster/"),
        "queue": ("sqs", ""),
        "registry": ("ecr", "repository/"),
        "secret": ("secretsmanager", "secret:"),
        "key": ("kms", "key/"),
        "audit_sink": ("execute-api", "api/"),
    }
    manifest: dict[str, Any] = {"environment": environment, "account_id": account, "region": REGION}
    for field, kind in {**SINGLES, **LISTS}.items():
        refs = []
        for index in range(3 if kind == "service" else 2 if kind == "subnet" else 1):
            service, prefix = resources[kind]
            scope_account = AUDIT if kind == "audit_sink" else account
            scope_env = "audit" if kind == "audit_sink" else environment
            arn = f"arn:aws:{service}:{REGION}:{scope_account}:{prefix}{scope_env}-{index}"
            digest = canonical_digest([arn, kind])
            inventory[arn] = ResourceBinding(arn, kind, scope_env, scope_account, REGION, digest)
            refs.append({"arn": arn, "configuration_digest": digest})
        manifest[field] = refs if field in LISTS else refs[0]
    policy = EnvironmentPolicy(
        PROD,
        NONPROD,
        AUDIT,
        REGION,
        approved_audit_arns=frozenset([manifest["audit_sink_ref"]["arn"]]),
    )
    seal(manifest)
    return manifest, policy, inventory


def seal(manifest: dict[str, Any]) -> None:
    manifest["configuration_digest"] = canonical_digest(
        {k: v for k, v in manifest.items() if k != "configuration_digest"}
    )


def rename_reference(
    manifest: dict[str, Any],
    inventory: dict[str, ResourceBinding],
    field: str,
    arn: str,
    **changes: Any,
) -> None:
    ref = manifest[field][0] if field in LISTS else manifest[field]
    old = ref["arn"]
    inventory[arn] = replace(inventory.pop(old), arn=arn, **changes)
    ref["arn"] = arn
    seal(manifest)


def control_inputs() -> tuple[dict[str, Any], EnvironmentPolicy, dict[str, ResourceBinding]]:
    _, policy, inventory = fixture()
    arn = f"arn:aws:acm:{REGION}:{NONPROD}:certificate/00000000-0000-0000-0000-000000000000"
    inventory[arn] = ResourceBinding(arn, "certificate", "dev", NONPROD, REGION, "a" * 64)
    parameters: dict[str, Any] = {
        "Environment": "dev",
        "ExpectedAccountId": NONPROD,
        "CertificateArn": arn,
        "DatabaseInstanceClass": "db.t4g.small",
        "DatabaseEngineVersion": "18.4",
        "DatabaseEngineFamily": "postgres18",
        "DatabaseStorageGiB": 20,
        "DatabaseMaxConnections": 100,
        "EcrS3PrefixListId": "pl-00000000",
    }
    return parameters, policy, inventory


def boundary_inputs() -> tuple[dict[str, Any], EnvironmentPolicy, dict[str, ResourceBinding]]:
    manifest, policy, inventory = fixture()
    parameters: dict[str, Any] = {
        "Environment": "dev",
        "ExpectedAccountId": NONPROD,
        "DatabaseResourceId": "db-00000000",
        "ApplicationSecretArn": manifest["secrets_refs"][0]["arn"],
        "DataKeyArn": f"arn:aws:kms:{REGION}:{NONPROD}:key/00000000-0000-0000-0000-000000000000",
        "WakeupQueueArn": manifest["queue_refs"][0]["arn"],
        "DeclaredServiceArns": [item["arn"] for item in manifest["service_refs"]],
        "RepositoryArns": [],
        "OidcProviderArn": f"arn:aws:iam::{NONPROD}:oidc-provider/"
        "token.actions.githubusercontent.com",
        "ApprovedDeployWorkflow": "kevon-dot/opentour-ai/.github/workflows/deploy.yml@" + "a" * 40,
        "ApprovedMigrationWorkflow": "kevon-dot/opentour-ai/.github/workflows/migrate.yml@"
        + "b" * 40,
    }
    db_arn = manifest["database_ref"]["arn"]
    inventory[db_arn] = replace(inventory[db_arn], provider_resource_id="db-00000000")
    for key, kind, region in (("DataKeyArn", "key", REGION), ("OidcProviderArn", "oidc", "")):
        arn = parameters[key]
        inventory[arn] = ResourceBinding(arn, kind, "dev", NONPROD, region, "a" * 64)
    for name in ("Bff", "Api", "Worker"):
        arn = f"arn:aws:ecr:{REGION}:{NONPROD}:repository/dev-{name.lower()}"
        parameters["RepositoryArns"].append(arn)
        inventory[arn] = ResourceBinding(arn, "registry", "dev", NONPROD, REGION, "a" * 64)
        arn = f"arn:aws:logs:{REGION}:{NONPROD}:log-group:/opentour/dev-{name.lower()}"
        parameters[name + "LogGroupArn"] = arn
        inventory[arn] = ResourceBinding(arn, "log", "dev", NONPROD, REGION, "a" * 64)
    return parameters, policy, inventory
