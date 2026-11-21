"""Generated Control challenge wire fixtures for OPE-184."""

import base64
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from opentour_control.runners.enrollment import (
    EnrollmentChallengeRequest,
    InstalledCell,
    InvalidEnrollmentChallenge,
    IssuedChallenge,
    enrollment_challenge_response,
    parse_enrollment_challenge_request,
)

NONCE_BYTES = bytes(range(32))
NONCE = base64.urlsafe_b64encode(NONCE_BYTES).decode().rstrip("=")
EXPIRES_AT = datetime(2026, 1, 1, 12, 0, 0, 123000, tzinfo=UTC)


@pytest.fixture
def challenge():
    return IssuedChallenge(
        id="défi / generated",
        installed=InstalledCell(
            organization_id="generated-org",
            environment_id="generated-env",
            cell_id="generated-cell",
            aws_account_id="123456789012",
            iam_role_arn="arn:aws:iam::123456789012:role/generated/path/Runner",
            region="us-east-1",
            deployment_mode="customer_controlled",
            installer_sha256=hashlib.sha256(b"generated-installer").hexdigest(),
            expected_iam_role_id=None,
        ),
        nonce_sha256=hashlib.sha256(NONCE_BYTES).hexdigest(),
        expires_at=EXPIRES_AT,
    )


@pytest.mark.parametrize("mode", ["initial", "rotation", "recovery"])
@pytest.mark.parametrize("revision", ["1", "9223372036854775807"])
def test_challenge_request_accepts_exact_modes_and_revision_bounds(mode, revision):
    body = json.dumps({"expected_environment_revision": revision, "mode": mode}).encode()
    assert parse_enrollment_challenge_request(body) == EnrollmentChallengeRequest(
        int(revision), mode
    )


@pytest.mark.parametrize(
    "revision",
    [
        0,
        1,
        1.0,
        True,
        None,
        "",
        "0",
        "01",
        " 1",
        "1\n",
        "+1",
        "-1",
        "1.0",
        "١",
        "9223372036854775808",
    ],
)
def test_challenge_request_rejects_noncanonical_revision(revision):
    body = json.dumps({"expected_environment_revision": revision, "mode": "initial"}).encode()
    with pytest.raises(InvalidEnrollmentChallenge, match="^ENROLLMENT_CHALLENGE_INVALID$"):
        parse_enrollment_challenge_request(body)


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"[]",
        b"null",
        b'"initial"',
        b'{"mode":"initial"}',
        b'{"expected_environment_revision":"1"}',
        b'{"expected_environment_revision":"1","mode":"manual"}',
        b'{"expected_environment_revision":"1","mode":[]}',
        b'{"expected_environment_revision":"1","mode":"initial","cell_id":"generated-cell"}',
        b'{"expected_environment_revision":"1","mode":"initial","mode":"initial"}',
        b'{"expected_environment_revision":"1",'
        b'"expected_environment_revision":"2","mode":"initial"}',
        b'{"expected_environment_revision":"1","mode":"initial"} null',
        '{"expected_environment_revision":"1","mode":"initial"}'.encode("utf-16"),
    ],
)
def test_challenge_request_rejects_unknown_missing_and_duplicate_fields(body):
    with pytest.raises(InvalidEnrollmentChallenge, match="^ENROLLMENT_CHALLENGE_INVALID$"):
        parse_enrollment_challenge_request(body)


@pytest.mark.parametrize(
    ("deployment_mode", "expected_role_id"),
    [("customer_controlled", None), ("opentour_managed", "generated-bound-role-id")],
)
def test_challenge_response_fresh_and_replay_have_original_exact_metadata(
    challenge, deployment_mode, expected_role_id
):
    challenge = replace(
        challenge,
        installed=replace(
            challenge.installed,
            deployment_mode=deployment_mode,
            expected_iam_role_id=expected_role_id,
        ),
        expires_at=EXPIRES_AT.astimezone(timezone(timedelta(hours=-5))),
    )
    fresh = enrollment_challenge_response(challenge, nonce=NONCE)
    replay = enrollment_challenge_response(challenge, nonce=None)

    assert fresh == {
        "challenge_id": "défi / generated",
        "nonce": NONCE,
        "secret_unavailable": False,
        "expires_at": "2026-01-01T12:00:00.123Z",
        "organization_id": "generated-org",
        "environment_id": "generated-env",
        "cell_id": "generated-cell",
        "aws_account_id": "123456789012",
        "iam_role_arn": "arn:aws:iam::123456789012:role/generated/path/Runner",
        "expected_iam_role_id": expected_role_id,
        "region": "us-east-1",
        "deployment_mode": deployment_mode,
        "installer_sha256": challenge.installed.installer_sha256,
        "proof_schema_version": "opentour-enrollment-v1",
    }
    assert replay == (fresh | {"nonce": None, "secret_unavailable": True})
    assert challenge.nonce_sha256 not in json.dumps(fresh)
    assert NONCE not in json.dumps(replay)


@pytest.mark.parametrize("nonce", ["", NONCE + "=", NONCE + "\n", NONCE[:-1] + "9", "A" * 43, True])
def test_challenge_response_rejects_malformed_or_changed_nonce(challenge, nonce):
    with pytest.raises(InvalidEnrollmentChallenge, match="^ENROLLMENT_CHALLENGE_INVALID$"):
        enrollment_challenge_response(challenge, nonce=nonce)


@pytest.mark.parametrize(
    "changes",
    [
        {"organization_id": ""},
        {"environment_id": "x" * 257},
        {"cell_id": 12},
        {"aws_account_id": "123456789012\n"},
        {"iam_role_arn": "arn:aws:iam::999999999999:role/generated/Runner"},
        {"iam_role_arn": "arn:aws:iam::123456789012:role/"},
        {"expected_iam_role_id": ""},
        {"region": "us-east-2"},
        {"deployment_mode": "shared"},
        {"installer_sha256": "X" * 64},
    ],
)
def test_challenge_response_rejects_unrepresentable_installation(challenge, changes):
    installed = replace(challenge.installed, **changes)
    with pytest.raises(InvalidEnrollmentChallenge, match="^ENROLLMENT_CHALLENGE_INVALID$"):
        enrollment_challenge_response(replace(challenge, installed=installed), nonce=None)


@pytest.mark.parametrize(
    "expires_at",
    [
        EXPIRES_AT.replace(tzinfo=None),
        EXPIRES_AT + timedelta(microseconds=1),
        datetime.min.replace(tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_challenge_response_refuses_lossy_or_invalid_expiry(challenge, expires_at):
    with pytest.raises(InvalidEnrollmentChallenge, match="^ENROLLMENT_CHALLENGE_INVALID$"):
        enrollment_challenge_response(replace(challenge, expires_at=expires_at), nonce=None)
