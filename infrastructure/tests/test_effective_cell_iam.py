"""Generated request checks for the retained evaluator store's explicit denies."""

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import replace

import pytest

from infrastructure.cell_installer import QualifiedProfile
from infrastructure.tests.test_cell_template import CELL_TEMPLATE, _resolve_storage_parameters

pytest_plugins = ("infrastructure.tests.test_cell_template",)

_EVALUATOR_BUCKET = "arn:aws:s3:::generated-cell-evaluator"
_TRAINER_BUCKET = "arn:aws:s3:::generated-cell-trainer"
_BUCKET_ARNS = {"EvaluatorStorage": _EVALUATOR_BUCKET, "TrainerStorage": _TRAINER_BUCKET}


def _bucket_policy_arn(value):
    if isinstance(value, str):
        return value
    if "Fn::GetAtt" in value:
        logical_id, attribute = value["Fn::GetAtt"]
        assert attribute == "Arn"
        return _BUCKET_ARNS[logical_id]
    substitution = re.fullmatch(r"\$\{(\w+)\.Arn\}(/\*)?", value["Fn::Sub"])
    assert substitution is not None
    return _BUCKET_ARNS[substitution[1]] + (substitution[2] or "")


def _matches(value, patterns, *, fold_case=False):
    if isinstance(patterns, str):
        patterns = [patterns]
    flags = re.IGNORECASE if fold_case else 0
    return any(
        re.fullmatch(re.escape(pattern).replace(r"\*", ".*").replace(r"\?", "."), value, flags)
        for pattern in patterns
    )


def _generated_result(policy, *, principal_arn, action, resource, identity_allow):
    """Apply only the exact role-conditioned deny form used by this fixture.

    The caller supplies a hypothetical identity allow as a control. For KMS the
    control also assumes independently approved key usage. A matching explicit
    deny overrides those assumptions. This is not a general IAM simulator.
    """
    for statement in policy:
        if statement["Effect"] != "Deny" or not _matches(
            action, statement["Action"], fold_case=True
        ):
            continue
        if not _matches(resource, statement["Resource"]):
            continue
        assert statement["Principal"] in ("*", {"AWS": "*"})
        assert set(statement["Condition"]) == {"ArnEquals"}
        condition = statement["Condition"]["ArnEquals"]
        assert set(condition) == {"aws:PrincipalArn"}
        denied_arn = condition["aws:PrincipalArn"]
        assert "*" not in denied_arn and "?" not in denied_arn
        if principal_arn == denied_arn:
            return "explicit-deny"
    return "fixture-allow" if identity_allow else "implicit-deny"


def _attached_s3_denies(policy, resource):
    attached = _BUCKET_ARNS[policy["Bucket"]["Ref"]]
    if resource != attached and not resource.startswith(attached + "/"):
        return []
    return [
        {
            **statement,
            "Resource": [_bucket_policy_arn(value) for value in statement["Resource"]]
            if isinstance(statement["Resource"], list)
            else _bucket_policy_arn(statement["Resource"]),
        }
        for statement in policy["PolicyDocument"]["Statement"]
    ]


@pytest.fixture(params=("opentour_managed", "customer_controlled"))
def evaluator_store_policy(
    request,
    parameters,
    environment_identity,
    approved_profiles,
    approved_release,
    approved_storage,
    compile_storage,
    key_policies,
):
    if request.param == "customer_controlled":
        parameters.update(
            mode=request.param,
            network_profile="fixture-customer-controlled-network",
            gateway_hostname=None,
            certificate_arn=None,
        )
        environment_identity["mode"] = request.param
        approved_profiles = replace(
            approved_profiles,
            network=QualifiedProfile(parameters["network_profile"], "3" * 64),
            gateway_hostname=None,
            certificate_arn=None,
        )
    template_bytes = CELL_TEMPLATE.read_bytes()
    release = replace(approved_release, template_sha256=hashlib.sha256(template_bytes).hexdigest())
    prepared = compile_storage(profiles=approved_profiles, release=release)
    template = json.loads(prepared.verified_template(template_bytes))
    compiled = json.loads(prepared.configuration)
    policy = _resolve_storage_parameters(
        template["Resources"]["EvaluatorStoragePolicy"]["Properties"],
        compiled["cloudformation_parameters"],
    )
    key_arn = compiled["cloudformation_parameters"]["EvaluatorStorageKmsKeyArn"]
    key_policy = key_policies[key_arn]
    assert hashlib.sha256(key_policy).hexdigest() == compiled["kms_key_policy_sha256"][key_arn]
    return policy, json.loads(key_policy)["Statement"], approved_storage, key_arn


@pytest.mark.parametrize(
    "action", ("s3:ListBucket", "s3:GetObject", "kms:Decrypt", "kms:ReEncryptFrom")
)
def test_evaluator_storage_request_is_explicitly_denied_despite_fixture_grant(
    evaluator_store_policy, action
):
    bucket_policy, key_policy, approved, key_arn = evaluator_store_policy
    if action.startswith("s3:"):
        resource = _EVALUATOR_BUCKET if action == "s3:ListBucket" else _EVALUATOR_BUCKET + "/case"
        policy = _attached_s3_denies(bucket_policy, resource)
    else:
        resource, policy = key_arn, key_policy
    trainer = approved.trainer.role_arn
    evaluator = approved.evaluator.role_arn
    assert (
        _generated_result(
            policy, principal_arn=trainer, action=action, resource=resource, identity_allow=True
        )
        == "explicit-deny"
    )
    assert (
        _generated_result(
            policy, principal_arn=evaluator, action=action, resource=resource, identity_allow=True
        )
        == "fixture-allow"
    )
    assert (
        _generated_result(
            policy, principal_arn=evaluator, action=action, resource=resource, identity_allow=False
        )
        == "implicit-deny"
    )


@pytest.mark.parametrize("drift", ("attached-bucket", "bucket-only", "object-only", "kms-action"))
def test_generated_requests_expose_relevant_deny_regressions(evaluator_store_policy, drift):
    bucket_policy, key_policy, approved, key_arn = evaluator_store_policy
    bucket_policy, key_policy = deepcopy(bucket_policy), deepcopy(key_policy)
    if drift == "kms-action":
        (deny,) = key_policy
        deny["Action"] = ["kms:ReEncryptFrom"]
        action, resource, policy = "kms:Decrypt", key_arn, key_policy
    else:
        action, resource = "s3:GetObject", _EVALUATOR_BUCKET + "/case"
        deny, *_ = bucket_policy["PolicyDocument"]["Statement"]
        if drift == "attached-bucket":
            bucket_policy["Bucket"] = {"Ref": "TrainerStorage"}
        elif drift == "bucket-only":
            deny["Resource"] = [deny["Resource"][0]]
        else:
            action, resource = "s3:ListBucket", _EVALUATOR_BUCKET
            deny["Resource"] = [deny["Resource"][1]]
        policy = _attached_s3_denies(bucket_policy, resource)
    assert (
        _generated_result(
            policy,
            principal_arn=approved.trainer.role_arn,
            action=action,
            resource=resource,
            identity_allow=True,
        )
        == "fixture-allow"
    )
