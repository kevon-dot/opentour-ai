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
_STORAGE_KEY_ARN_PATTERN = (
    r"arn:aws:kms:(?!cn-)[a-z]{2}-[a-z]+-[1-9][0-9]*:[0-9]{12}:key/" + _SINGLE_REGION_KEY_ID
)
_STORAGE_LAYOUT = {
    "trainer": ("TrainerStorage", "evaluator"),
    "evaluator": ("EvaluatorStorage", "trainer"),
    "evidence": ("EvidenceStorage", "trainer"),
    "artifact": ("ArtifactStorage", None),
}
_STORAGE_KEY_PARAMETERS = {
    name: f"{logical_id}KmsKeyArn" for name, (logical_id, _) in _STORAGE_LAYOUT.items()
}
_STORAGE_ROLE_PARAMETERS = {"trainer": "TrainerRoleArn", "evaluator": "EvaluatorRoleArn"}
_Arn = str | dict[str, str]
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

    def verified_template(self, template: bytes) -> bytes:
        """Return the unchanged common template pinned by this install's verified release.

        Templates and cell-specific configuration remain separate. This check assumes the
        caller obtained the install from the trusted compiler with verified release inputs.
        It does not validate the template's resource coverage or authorize a change set.
        """
        try:
            configuration = json.loads(self.configuration)
            if (
                not isinstance(configuration, dict)
                or _prepared_configuration(configuration) != self
            ):
                raise ValueError
        except (TypeError, ValueError, RecursionError):
            raise CellInstallRefused("CELL_INSTALL_CONFIGURATION_INVALID") from None

        expected = configuration.get("template_sha256")
        if (
            not isinstance(expected, str)
            or SHA256.fullmatch(expected) is None
            or not isinstance(template, bytes)
            or not template
            or hashlib.sha256(template).hexdigest() != expected
        ):
            raise CellInstallRefused("CELL_INSTALL_TEMPLATE_MISMATCH")
        return template


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
class RetainedStorageKeyBinding:
    """The exact single-Region key and readback for a retained data store."""

    kms_key_arn: str
    key_policy_sha256: str


@dataclass(frozen=True)
class ApprovedCellStorage:
    """Exact role and purpose-bound key choices from the qualified KMS profile."""

    kms_policy_sha256: str
    trainer: WorkloadStorageBinding
    evaluator: WorkloadStorageBinding
    evidence: RetainedStorageKeyBinding
    artifact: RetainedStorageKeyBinding


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
    """Compile the validated retained-storage parameters, resources, and input digests.

    Deployment authority supplies inventory and the default GetKeyPolicy text encoded
    unchanged as UTF-8, indexed by the exact ARN used in the authenticated request.
    The profile pins those bytes independently. Bucket policies only deny unwanted access.
    Approved data access roles, key ownership and policy freshness still need provider
    verification when deploying the cell.
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
    bindings: dict[str, WorkloadStorageBinding | RetainedStorageKeyBinding] = {
        "trainer": storage.trainer,
        "evaluator": storage.evaluator,
        "evidence": storage.evidence,
        "artifact": storage.artifact,
    }
    roles = {"trainer": storage.trainer.role_arn, "evaluator": storage.evaluator.role_arn}
    # IAM role names are account-wide and case-insensitive, independent of path.
    role_names = {role.rsplit("/", 1)[-1].casefold() for role in roles.values()}
    keys = {name: binding.kms_key_arn for name, binding in bindings.items()}
    if (
        storage.kms_policy_sha256 != profiles.kms_policy.sha256
        or len(role_names) != 2
        or len(set(keys.values())) != len(bindings)
    ):
        raise CellInstallRefused("CELL_INSTALL_STORAGE_BINDING_MISMATCH")

    account = parameters["account_id"]
    region = parameters["region"]
    key_pattern = re.escape(f"arn:aws:kms:{region}:{account}:key/") + _SINGLE_REGION_KEY_ID
    role_pattern = re.escape(f"arn:aws:iam::{account}:role/") + _ROLE_PATH_AND_NAME
    if any(re.fullmatch(key_pattern, key) is None for key in keys.values()) or any(
        re.fullmatch(role_pattern, role) is None for role in roles.values()
    ):
        raise CellInstallRefused("CELL_INSTALL_STORAGE_SCOPE_MISMATCH")
    references = dict.fromkeys(keys.values(), "key") | dict.fromkeys(roles.values(), "role")

    configuration["resource_configuration_sha256"].update(
        _qualified_resources(references, inventory, parameters["environment_id"], account, region)
    )
    key_policy_digests = {}
    for name, (_, denied) in _STORAGE_LAYOUT.items():
        binding = bindings[name]
        key_policy_digests[binding.kms_key_arn] = _qualified_key_policy(
            binding.key_policy_sha256,
            key_policies.get(binding.kms_key_arn),
            None if denied is None else roles[denied],
        )
    configuration["kms_key_policy_sha256"] = key_policy_digests
    configuration["cloudformation_parameters"] = {
        **{parameter: keys[name] for name, parameter in _STORAGE_KEY_PARAMETERS.items()},
        **{parameter: roles[name] for name, parameter in _STORAGE_ROLE_PARAMETERS.items()},
    }
    configuration["cloudformation_resources"] = _storage_resources(keys, roles)
    return _prepared_configuration(configuration)


def _unique_policy_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("Duplicate key policy property")
    return value


def _qualified_key_policy(
    expected_sha256: str, policy: bytes | None, denied_role_arn: str | None
) -> str:
    if (
        not isinstance(policy, bytes)
        or not 0 < len(policy) <= 131_072
        or hashlib.sha256(policy).hexdigest() != expected_sha256
    ):
        raise CellInstallRefused("CELL_INSTALL_KEY_POLICY_UNQUALIFIED")
    try:
        document = json.loads(policy.decode("utf-8"), object_pairs_hook=_unique_policy_object)
    except (ValueError, RecursionError):
        raise CellInstallRefused("CELL_INSTALL_KEY_POLICY_UNQUALIFIED") from None
    if not isinstance(document, dict) or document.get("Version") != "2012-10-17":
        raise CellInstallRefused("CELL_INSTALL_KEY_POLICY_UNQUALIFIED")
    statements = document.get("Statement")
    if (
        not isinstance(statements, list)
        or not statements
        or any(not isinstance(statement, dict) for statement in statements)
    ):
        raise CellInstallRefused("CELL_INSTALL_KEY_POLICY_UNQUALIFIED")
    if denied_role_arn is None:
        return expected_sha256

    matching_actions: set[str] = set()
    for statement in statements:
        if (
            set(statement) - {"Sid", "Effect", "Principal", "Action", "Resource", "Condition"}
            or statement.get("Effect") != "Deny"
            or statement.get("Principal") not in ("*", {"AWS": "*"})
            or statement.get("Resource") != "*"
            or statement.get("Condition") != {"ArnEquals": {"aws:PrincipalArn": denied_role_arn}}
        ):
            continue
        actions = statement.get("Action")
        if isinstance(actions, str):
            actions = [actions]
        if isinstance(actions, list) and all(isinstance(action, str) for action in actions):
            matching_actions.update(actions)
    if not _KMS_CROSS_ROLE_READ_ACTIONS <= matching_actions:
        raise CellInstallRefused("CELL_INSTALL_CROSS_ROLE_DECRYPT_DENY_MISSING")
    return expected_sha256


def retained_storage_template_section() -> dict[str, dict[str, object]]:
    """Generate the static template section using deployment-internal ARN parameters."""
    key_refs = {name: {"Ref": parameter} for name, parameter in _STORAGE_KEY_PARAMETERS.items()}
    role_refs = {name: {"Ref": parameter} for name, parameter in _STORAGE_ROLE_PARAMETERS.items()}
    return {
        "Parameters": {
            **{
                ref["Ref"]: {
                    "Type": "String",
                    "AllowedPattern": _STORAGE_KEY_ARN_PATTERN,
                }
                for ref in key_refs.values()
            },
            **{
                ref["Ref"]: {
                    "Type": "String",
                    "AllowedPattern": r"arn:aws:iam::[0-9]{12}:role/" + _ROLE_PATH_AND_NAME,
                }
                for ref in role_refs.values()
            },
        },
        "Resources": _storage_resources(key_refs, role_refs),
    }


def _storage_resources(keys: Mapping[str, _Arn], roles: Mapping[str, _Arn]) -> dict[str, object]:
    return {
        name: resource
        for store, (logical_id, denied) in _STORAGE_LAYOUT.items()
        for name, resource in _private_bucket_resources(
            logical_id, keys[store], None if denied is None else roles[denied]
        ).items()
    }


def _private_bucket_resources(
    logical_id: str, kms_key_arn: _Arn, denied_role_arn: _Arn | None
) -> dict[str, object]:
    retain = {"DeletionPolicy": "Retain", "UpdateReplacePolicy": "Retain"}
    bucket_arn = {"Fn::GetAtt": [logical_id, "Arn"]}
    objects_arn = {"Fn::Sub": "${" + logical_id + ".Arn}/*"}
    explicit_deny = {"Effect": "Deny", "Principal": "*"}
    statements = []
    if denied_role_arn is not None:
        statements.append(
            {
                "Sid": "DenyOppositeWorkload",
                **explicit_deny,
                "Action": "s3:*",
                "Resource": [bucket_arn, objects_arn],
                "Condition": {"ArnEquals": {"aws:PrincipalArn": denied_role_arn}},
            }
        )
    statements.extend(
        [
            {
                "Sid": "RequireKmsForObjectWrites",
                **explicit_deny,
                "Action": "s3:PutObject",
                "Resource": objects_arn,
                "Condition": {"StringNotEquals": {"s3:x-amz-server-side-encryption": "aws:kms"}},
            },
            {
                "Sid": "RequireExactKeyForObjectWrites",
                **explicit_deny,
                "Action": "s3:PutObject",
                "Resource": objects_arn,
                "Condition": {
                    "StringNotEquals": {
                        "s3:x-amz-server-side-encryption-aws-kms-key-id": kms_key_arn
                    }
                },
            },
        ]
    )
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
                                "KMSMasterKeyID": kms_key_arn,
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
                    "Statement": statements,
                },
            },
        },
    }
