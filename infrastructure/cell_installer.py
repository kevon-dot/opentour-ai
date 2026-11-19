"""OPE-181 offline installer binding and cell resource compilation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from referencing import Resource

from infrastructure.environment_contract import SHA256, ResourceBinding
from tools.schema_validate import read_json, schema_registry, valid_instance

_ROOT = Path(__file__).resolve().parents[1]
_PARAMETERS = _ROOT / "infrastructure/cloudformation/parameters.schema.json"
_IDENTITY = _ROOT / "packages/contracts/platform/environment-profile.schema.json"
_PARAMETERS_URI = "urn:opentour:cell-installer-parameters:1.0"
_IDENTITY_URI = "urn:opentour:environment-profile:1.0"
_IDENTITY_FIELDS = {
    "organization_id": "organization_id",
    "environment_id": "environment_id",
    "cell_id": "cell_id",
    "mode": "mode",
    "aws_account_id": "account_id",
    "region": "region",
}
_WORKLOADS = ("controller", "evaluator", "trainer")
_SINGLE_REGION_KEY_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_ROLE_PATH_AND_NAME = r"(?:[A-Za-z0-9_+=,.@/-]{1,510}/)?[A-Za-z0-9_+=,.@-]{1,64}"
_KMS_CROSS_ROLE_READ_ACTIONS = frozenset({"kms:Decrypt", "kms:ReEncryptFrom"})


class CellInstallRefused(ValueError):
    """The input cannot be bound to the independently approved deployment."""


@dataclass(frozen=True)
class QualifiedProfile:
    """An exact registry key and the digest of its independently qualified content."""

    key: str
    sha256: str


@dataclass(frozen=True)
class ApprovedCellProfiles:
    """Profile choices and network references authorized together for the cell."""

    control_endpoint: QualifiedProfile
    kms_policy: QualifiedProfile
    network: QualifiedProfile
    subnet_ids: tuple[str, ...]
    security_group_ids: tuple[str, ...]
    gateway_hostname: str | None
    certificate_arn: str | None
    retention_policy_id: str
    retention_policy_revision: str


@dataclass(frozen=True)
class VerifiedCellRelease:
    """Pin a release already verified by the release authority."""

    release_sha256: str
    template_sha256: str
    parameter_schema_sha256: str
    images: Mapping[str, str]


@dataclass(frozen=True)
class PreparedCellInstall:
    """Immutable input for subsequent template compilation and authorization."""

    configuration: bytes
    configuration_sha256: str


def prepare_cell_install(
    requested: object,
    *,
    approved_identity: object,
    profiles: ApprovedCellProfiles,
    release: VerifiedCellRelease,
    inventory: Mapping[str, ResourceBinding],
) -> PreparedCellInstall:
    """Bind closed parameters to independent identity, profile, release and resource inputs.

    The caller supplies current authority from trusted registries and a separately verified
    release. The inventory uses the immutable tenant environment ID as its environment scope.
    The result is an offline input for compilation. Template bytes, live resources and
    execution authority still require their own checks.
    """
    parameter_bytes = _PARAMETERS.read_bytes()
    if hashlib.sha256(parameter_bytes).hexdigest() != release.parameter_schema_sha256:
        raise CellInstallRefused("CELL_INSTALL_SCHEMA_MISMATCH")
    registry = schema_registry(_ROOT).with_resources(
        (
            (_PARAMETERS_URI, Resource.from_contents(json.loads(parameter_bytes))),
            (_IDENTITY_URI, Resource.from_contents(read_json(_IDENTITY))),
        )
    )
    # Work on one snapshot. The result cannot be changed through the caller's containers.
    requested = deepcopy(requested)
    approved_identity = deepcopy(approved_identity)
    if not isinstance(requested, dict) or not valid_instance(_PARAMETERS_URI, requested, registry):
        raise CellInstallRefused("CELL_INSTALL_PARAMETERS_INVALID")
    if not valid_instance(_IDENTITY_URI, approved_identity, registry):
        raise CellInstallRefused("CELL_INSTALL_IDENTITY_INVALID")
    if approved_identity != {
        identity: requested[parameter] for identity, parameter in _IDENTITY_FIELDS.items()
    }:
        raise CellInstallRefused("CELL_INSTALL_IDENTITY_MISMATCH")

    profile_fields = {
        "control_endpoint": profiles.control_endpoint,
        "kms_policy_profile": profiles.kms_policy,
        "network_profile": profiles.network,
    }
    for field, profile in profile_fields.items():
        if requested[field] != profile.key or SHA256.fullmatch(profile.sha256) is None:
            raise CellInstallRefused("CELL_INSTALL_PROFILE_UNQUALIFIED")

    network_and_retention = {
        "subnet_ids": list(profiles.subnet_ids),
        "security_group_ids": list(profiles.security_group_ids),
        "gateway_hostname": profiles.gateway_hostname,
        "certificate_arn": profiles.certificate_arn,
        "retention_policy_ref": {
            "policy_id": profiles.retention_policy_id,
            "revision": profiles.retention_policy_revision,
        },
    }
    if any(requested[field] != approved for field, approved in network_and_retention.items()):
        raise CellInstallRefused("CELL_INSTALL_PROFILE_MISMATCH")

    images = dict(release.images)
    if (
        SHA256.fullmatch(release.release_sha256) is None
        or SHA256.fullmatch(release.template_sha256) is None
        or images.keys() != set(_WORKLOADS)
    ):
        raise CellInstallRefused("CELL_INSTALL_RELEASE_INVALID")

    account = requested["account_id"]
    region = requested["region"]
    scope = f"{region}:{account}"
    references = {
        **{f"arn:aws:ec2:{scope}:subnet/{name}": "subnet" for name in profiles.subnet_ids},
        **{
            f"arn:aws:ec2:{scope}:security-group/{name}": "network_policy"
            for name in profiles.security_group_ids
        },
    }
    if profiles.certificate_arn is not None:
        if not profiles.certificate_arn.startswith(f"arn:aws:acm:{scope}:certificate/"):
            raise CellInstallRefused("CELL_INSTALL_RESOURCE_SCOPE_MISMATCH")
        references[profiles.certificate_arn] = "certificate"

    image_host = f"{account}.dkr.ecr.{region}.amazonaws.com/"
    for workload in _WORKLOADS:
        image = images[workload]
        expected_digest = requested[f"{workload}_digest"]
        location = re.fullmatch(
            re.escape(image_host)
            + r"(?P<repo>[a-z0-9]+(?:[._/-][a-z0-9]+)*)@sha256:"
            + re.escape(expected_digest),
            image,
        )
        if location is None:
            raise CellInstallRefused("CELL_INSTALL_IMAGE_MISMATCH")
        references[f"arn:aws:ecr:{scope}:repository/{location['repo']}"] = "registry"

    resources = _qualified_resources(
        references, inventory, requested["environment_id"], account, region
    )
    return _prepared_configuration(
        {
            "parameters": requested,
            "release_sha256": release.release_sha256,
            "template_sha256": release.template_sha256,
            "parameter_schema_sha256": release.parameter_schema_sha256,
            "profile_sha256": {field: profile.sha256 for field, profile in profile_fields.items()},
            "images": images,
            "resource_configuration_sha256": resources,
        }
    )


def _qualified_resources(
    references: Mapping[str, str],
    inventory: Mapping[str, ResourceBinding],
    environment_id: str,
    account: str,
    region: str,
) -> dict[str, str]:
    digests: dict[str, str] = {}
    for arn, kind in references.items():
        binding = inventory.get(arn)
        if (
            binding is None
            or binding.arn != arn
            or binding.kind != kind
            or binding.environment != environment_id
            or binding.account_id != account
            or binding.region != region
            or SHA256.fullmatch(binding.configuration_digest) is None
        ):
            raise CellInstallRefused("CELL_INSTALL_RESOURCE_UNQUALIFIED")
        digests[arn] = binding.configuration_digest
    return digests


def _prepared_configuration(value: object) -> PreparedCellInstall:
    configuration = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return PreparedCellInstall(configuration, hashlib.sha256(configuration).hexdigest())


@dataclass(frozen=True)
class WorkloadStorageBinding:
    """A qualified single-Region encryption key and workload role."""

    kms_key_arn: str
    role_arn: str
    key_policy_sha256: str


@dataclass(frozen=True)
class ApprovedCellStorage:
    """Exact role/key choices from the independently qualified KMS profile."""

    kms_policy_sha256: str
    trainer: WorkloadStorageBinding
    evaluator: WorkloadStorageBinding


def compile_cell_storage(
    requested: object,
    *,
    approved_identity: object,
    profiles: ApprovedCellProfiles,
    release: VerifiedCellRelease,
    inventory: Mapping[str, ResourceBinding],
    storage: ApprovedCellStorage,
    key_policies: Mapping[str, bytes],
) -> PreparedCellInstall:
    """Compile a resource fragment with its immutable input and trusted resource digests.

    Deployment authority supplies inventory and the default GetKeyPolicy text encoded
    unchanged as UTF-8, indexed by the exact ARN used in the authenticated request.
    The profile pins those bytes independently. This fragment is a compiler input.
    Live authorization and policy freshness need verification at deployment.
    """
    prepared = prepare_cell_install(
        requested,
        approved_identity=approved_identity,
        profiles=profiles,
        release=release,
        inventory=inventory,
    )
    configuration = json.loads(prepared.configuration)
    parameters = configuration["parameters"]
    if (
        storage.kms_policy_sha256 != profiles.kms_policy.sha256
        or storage.trainer.role_arn == storage.evaluator.role_arn
        or storage.trainer.kms_key_arn == storage.evaluator.kms_key_arn
    ):
        raise CellInstallRefused("CELL_INSTALL_STORAGE_BINDING_MISMATCH")

    account = parameters["account_id"]
    region = parameters["region"]
    key_pattern = re.escape(f"arn:aws:kms:{region}:{account}:key/") + _SINGLE_REGION_KEY_ID
    role_pattern = re.escape(f"arn:aws:iam::{account}:role/") + _ROLE_PATH_AND_NAME
    references: dict[str, str] = {}
    for binding in (storage.trainer, storage.evaluator):
        if (
            re.fullmatch(key_pattern, binding.kms_key_arn) is None
            or re.fullmatch(role_pattern, binding.role_arn) is None
        ):
            raise CellInstallRefused("CELL_INSTALL_STORAGE_SCOPE_MISMATCH")
        references[binding.kms_key_arn] = "key"
        references[binding.role_arn] = "role"

    configuration["resource_configuration_sha256"].update(
        _qualified_resources(references, inventory, parameters["environment_id"], account, region)
    )
    configuration["kms_key_policy_sha256"] = {
        binding.kms_key_arn: _qualified_key_policy(
            binding, opposite.role_arn, key_policies.get(binding.kms_key_arn)
        )
        for binding, opposite in (
            (storage.trainer, storage.evaluator),
            (storage.evaluator, storage.trainer),
        )
    }
    configuration["cloudformation_resources"] = {
        **_private_bucket_resources("TrainerStorage", storage.trainer, storage.evaluator),
        **_private_bucket_resources("EvaluatorStorage", storage.evaluator, storage.trainer),
    }
    return _prepared_configuration(configuration)


def _unique_policy_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Duplicate key policy property")
    return value


def _qualified_key_policy(
    binding: WorkloadStorageBinding, opposite_role: str, policy: bytes | None
) -> str:
    if (
        not isinstance(policy, bytes)
        or not 0 < len(policy) <= 131_072
        or hashlib.sha256(policy).hexdigest() != binding.key_policy_sha256
    ):
        raise CellInstallRefused("CELL_INSTALL_KEY_POLICY_UNQUALIFIED")
    try:
        document = json.loads(policy.decode("utf-8"), object_pairs_hook=_unique_policy_object)
    except (ValueError, RecursionError):
        raise CellInstallRefused("CELL_INSTALL_KEY_POLICY_UNQUALIFIED") from None
    if not isinstance(document, dict) or document.get("Version") != "2012-10-17":
        raise CellInstallRefused("CELL_INSTALL_KEY_POLICY_UNQUALIFIED")
    statements = document.get("Statement")
    if not isinstance(statements, list):
        raise CellInstallRefused("CELL_INSTALL_KEY_POLICY_UNQUALIFIED")

    matching_actions: set[str] = set()
    for statement in statements:
        if (
            not isinstance(statement, dict)
            or set(statement) - {"Sid", "Effect", "Principal", "Action", "Resource", "Condition"}
            or statement.get("Effect") != "Deny"
            or statement.get("Principal") not in ("*", {"AWS": "*"})
            or statement.get("Resource") != "*"
            or statement.get("Condition") != {"ArnEquals": {"aws:PrincipalArn": opposite_role}}
        ):
            continue
        actions = statement.get("Action")
        if isinstance(actions, str):
            actions = [actions]
        if isinstance(actions, list) and all(isinstance(action, str) for action in actions):
            matching_actions.update(actions)
    if not _KMS_CROSS_ROLE_READ_ACTIONS <= matching_actions:
        raise CellInstallRefused("CELL_INSTALL_CROSS_ROLE_DECRYPT_DENY_MISSING")
    return binding.key_policy_sha256


def _private_bucket_resources(
    logical_id: str, binding: WorkloadStorageBinding, opposite: WorkloadStorageBinding
) -> dict[str, object]:
    retain = {"DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain"}
    bucket_arn = {"Fn::GetAtt": [logical_id, "Arn"]}
    objects_arn = {"Fn::Sub": "${" + logical_id + ".Arn}/*"}
    explicit_deny = {"Effect": "Deny", "Principal": "*"}
    return {
        logical_id: {
            "Type": "AWS::S3::Bucket",
            **retain,
            "Properties": {
                "BucketEncryption": {
                    "ServerSideEncryptionConfiguration": [
                        {
                            "ServerSideEncryptionByDefault": {
                                "SSEAlgorithm": "aws:kms",
                                "KMSMasterKeyID": binding.kms_key_arn,
                            },
                            "BucketKeyEnabled": False,
                        }
                    ]
                },
                "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "BlockPublicPolicy": True,
                    "IgnorePublicAcls": True,
                    "RestrictPublicBuckets": True,
                },
                "VersioningConfiguration": {"Status": "Enabled"},
            },
        },
        f"{logical_id}Policy": {
            "Type": "AWS::S3::BucketPolicy",
            **retain,
            "Properties": {
                "Bucket": {"Ref": logical_id},
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "DenyOppositeWorkload",
                            **explicit_deny,
                            "Action": "s3:*",
                            "Resource": [bucket_arn, objects_arn],
                            "Condition": {"ArnEquals": {"aws:PrincipalArn": opposite.role_arn}},
                        },
                        {
                            "Sid": "RequireKmsForObjectWrites",
                            **explicit_deny,
                            "Action": "s3:PutObject",
                            "Resource": objects_arn,
                            "Condition": {
                                "StringNotEquals": {"s3:x-amz-server-side-encryption": "aws:kms"}
                            },
                        },
                        {
                            "Sid": "RequireExactKeyForObjectWrites",
                            **explicit_deny,
                            "Action": "s3:PutObject",
                            "Resource": objects_arn,
                            "Condition": {
                                "StringNotEquals": {
                                    "s3:x-amz-server-side-encryption-aws-kms-key-id": (
                                        binding.kms_key_arn
                                    )
                                }
                            },
                        },
                    ],
                },
            },
        },
    }
