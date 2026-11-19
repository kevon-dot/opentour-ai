"""OPE-181 offline binding of tenant-cell installer inputs."""

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
            re.escape(image_host) + r"(?P<repo>[a-z0-9]+(?:[._/-][a-z0-9]+)*)@sha256:"
            + re.escape(expected_digest),
            image,
        )
        if location is None:
            raise CellInstallRefused("CELL_INSTALL_IMAGE_MISMATCH")
        references[f"arn:aws:ecr:{scope}:repository/{location['repo']}"] = "registry"

    resources: dict[str, str] = {}
    for arn, kind in references.items():
        binding = inventory.get(arn)
        if (
            binding is None
            or binding.arn != arn
            or binding.kind != kind
            or binding.environment != requested["environment_id"]
            or binding.account_id != account
            or binding.region != region
            or SHA256.fullmatch(binding.configuration_digest) is None
        ):
            raise CellInstallRefused("CELL_INSTALL_RESOURCE_UNQUALIFIED")
        resources[arn] = binding.configuration_digest

    configuration = json.dumps(
        {
            "parameters": requested,
            "release_sha256": release.release_sha256,
            "template_sha256": release.template_sha256,
            "parameter_schema_sha256": release.parameter_schema_sha256,
            "profile_sha256": {field: profile.sha256 for field, profile in profile_fields.items()},
            "images": images,
            "resource_configuration_sha256": resources,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return PreparedCellInstall(configuration, hashlib.sha256(configuration).hexdigest())
