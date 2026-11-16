"""Static source/policy assertions only; real AWS deny observations remain AC07."""

import json
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "cloudformation/audit-journal.yaml"


@pytest.fixture
def template():
    return json.loads(TEMPLATE.read_text())


def actions(template, policy, effect):
    statements = template["Resources"][policy]["Properties"]["PolicyDocument"]["Statement"]
    return {
        a for statement in statements if statement["Effect"] == effect for a in statement["Action"]
    }


def test_independent_account_region_and_role_rules_are_mandatory(template):
    parameters = template["Parameters"]
    assert not any("Default" in definition for definition in parameters.values())
    assertions = template["Rules"]["ApprovedIndependentDestination"]["Assertions"]
    pairs = [a["Assert"].get("Fn::Equals") for a in assertions]
    assert [{"Ref": "ExpectedAuditAccountId"}, {"Ref": "AWS::AccountId"}] in pairs
    assert [{"Ref": "ApprovedRegion"}, {"Ref": "AWS::Region"}] in pairs
    negative = [
        a["Assert"]["Fn::Not"][0]["Fn::Equals"] for a in assertions if "Fn::Not" in a["Assert"]
    ]
    assert [{"Ref": "ExpectedAuditAccountId"}, {"Ref": "ControlAccountId"}] in negative
    assert [{"Ref": "ExpectedAuditAccountId"}, {"Ref": "NonproductionAccountId"}] in negative


@pytest.mark.parametrize("name", ["JournalTable", "ContentBucket", "SigningKey", "EncryptionKey"])
def test_stateful_resources_survive_delete_and_replace(template, name):
    resource = template["Resources"][name]
    assert resource["DeletionPolicy"] == resource["UpdateReplacePolicy"] == "Retain"


def test_dynamodb_pitr_encryption_and_no_authority_ttl(template):
    table = template["Resources"]["JournalTable"]["Properties"]
    assert table["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True
    assert table["DeletionProtectionEnabled"] is True
    assert "TimeToLiveSpecification" not in table and "GlobalSecondaryIndexes" not in table
    assert table["SSESpecification"] == {
        "SSEEnabled": True,
        "SSEType": "KMS",
        "KMSMasterKeyId": {"Fn::GetAtt": ["EncryptionKey", "Arn"]},
    }


def test_object_lock_has_explicit_approved_default_and_no_public_content(template):
    bucket = template["Resources"]["ContentBucket"]["Properties"]
    assert all(bucket["PublicAccessBlockConfiguration"].values())
    assert bucket["VersioningConfiguration"]["Status"] == "Enabled"
    assert bucket["ObjectLockEnabled"] is True
    assert bucket["ObjectLockConfiguration"]["Rule"]["DefaultRetention"] == {
        "Mode": {"Ref": "RetentionMode"},
        "Days": {"Ref": "RetentionDays"},
    }
    statements = template["Resources"]["ContentBucketPolicy"]["Properties"]["PolicyDocument"][
        "Statement"
    ]
    tls = next(s for s in statements if s["Sid"] == "RequireTLS")
    assert tls["Effect"] == "Deny" and tls["Condition"] == {
        "Bool": {"aws:SecureTransport": "false"}
    }
    override = next(s for s in statements if s["Sid"] == "ForbidPerObjectRetentionOverride")
    assert override["Effect"] == "Deny"


@pytest.mark.parametrize(
    "action",
    [
        "dynamodb:DeleteItem",
        "dynamodb:DeleteTable",
        "dynamodb:UpdateTable",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:BypassGovernanceRetention",
        "s3:PutObjectRetention",
        "kms:DisableKey",
        "kms:ScheduleKeyDeletion",
        "iam:PassRole",
    ],
)
def test_append_role_has_explicit_destructive_denials(template, action):
    assert action in actions(template, "AppendPolicy", "Deny")
    assert action not in actions(template, "AppendPolicy", "Allow")


@pytest.mark.parametrize(
    "action",
    [
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "s3:PutObject",
        "s3:DeleteObjectVersion",
        "kms:Sign",
        "kms:PutKeyPolicy",
        "iam:PassRole",
    ],
)
def test_recovery_role_cannot_append_sign_or_administer(template, action):
    assert action in actions(template, "RecoveryPolicy", "Deny")
    assert action not in actions(template, "RecoveryPolicy", "Allow")


def test_keys_have_distinct_purpose_and_approved_role_bindings(template):
    resources = template["Resources"]
    signing = resources["SigningKey"]["Properties"]
    encryption = resources["EncryptionKey"]["Properties"]
    assert (signing["KeySpec"], signing["KeyUsage"]) == ("ECC_NIST_P256", "SIGN_VERIFY")
    assert encryption["KeyUsage"] == "ENCRYPT_DECRYPT"
    assert signing["MultiRegion"] is encryption["MultiRegion"] is False
    allowed = [s for s in signing["KeyPolicy"]["Statement"] if "kms:Sign" in s["Action"]]
    assert len(allowed) == 1
    assert allowed[0]["Principal"] == {"AWS": {"Fn::GetAtt": ["AppendRole", "Arn"]}}
    assert allowed[0]["Condition"] == {"StringEquals": {"kms:SigningAlgorithm": "ECDSA_SHA_256"}}


def test_control_roles_get_no_direct_storage_or_key_principal(template):
    resources = template["Resources"]
    for name in ["SigningKey", "EncryptionKey", "JournalTable", "ContentBucketPolicy"]:
        assert "ControlAccountId" not in json.dumps(resources[name])
    mutations = resources["JournalTable"]["Properties"]["ResourcePolicy"]["PolicyDocument"][
        "Statement"
    ]
    append = next(s for s in mutations if s["Sid"] == "OnlyJournalMutates")
    assert append["Effect"] == "Deny" and append["Principal"] == "*"
    assert {"Fn::GetAtt": ["AppendRole", "Arn"]} in append["Condition"]["ArnNotEquals"][
        "aws:PrincipalArn"
    ]


def test_no_mutable_wildcard_allow_or_implicit_provider_qualification(template):
    for name in ["AppendPolicy", "RecoveryPolicy"]:
        assert not any("*" in action for action in actions(template, name, "Allow"))
    assert template["Metadata"]["OpenTour"]["product_acceptance"] == "NOT_RUN"
    assert template["Parameters"]["DeploymentMode"]["AllowedValues"] == [
        "customer_controlled",
        "opentour_managed",
    ]
