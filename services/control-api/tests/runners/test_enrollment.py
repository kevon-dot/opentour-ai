"""Local enrollment proof conformance using only generated data and keys."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from opentour_control.runners.enrollment import (
    InstalledCell,
    InvalidEnrollmentProof,
    IssuedChallenge,
    validate_enrollment_proof,
)

NOW = datetime(2026, 11, 18, 10, 0, 0, tzinfo=UTC)
NONCE_BYTES = bytes(range(32))


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def fixture_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@pytest.fixture
def installed() -> InstalledCell:
    return InstalledCell(
        organization_id="fixture-org",
        environment_id="fixture-env",
        cell_id="fixture-cell",
        aws_account_id="000000000001",
        iam_role_arn="arn:aws:iam::000000000001:role/cell/fixture-role",
        region="us-east-1",
        deployment_mode="opentour_managed",
        installer_sha256="a" * 64,
        expected_iam_role_id=None,
    )


@pytest.fixture
def challenge(installed: InstalledCell) -> IssuedChallenge:
    return IssuedChallenge(
        "fixture-challenge",
        installed,
        hashlib.sha256(NONCE_BYTES).hexdigest(),
        NOW + timedelta(seconds=75),
    )


def proof_for(installed: InstalledCell, challenge: IssuedChallenge) -> dict[str, Any]:
    key = ec.derive_private_key(7, ec.SECP256R1())
    public = key.public_key().public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": b64(public.x.to_bytes(32)),
        "y": b64(public.y.to_bytes(32)),
    }
    transcript = {
        "protocol": "opentour-enrollment-v1",
        "challenge_id": challenge.id,
        "nonce": b64(NONCE_BYTES),
        "organization_id": installed.organization_id,
        "environment_id": installed.environment_id,
        "cell_id": installed.cell_id,
        "aws_account_id": installed.aws_account_id,
        "iam_role_arn": installed.iam_role_arn,
        "expected_iam_role_id": installed.expected_iam_role_id,
        "region": installed.region,
        "deployment_mode": installed.deployment_mode,
        "installer_sha256": installed.installer_sha256,
        "runner_jwk_thumbprint": b64(hashlib.sha256(fixture_json(jwk)).digest()),
    }
    canonical = fixture_json(transcript)
    r, s = utils.decode_dss_signature(key.sign(canonical, ec.ECDSA(hashes.SHA256())))
    query = urlencode(
        {
            "Action": "GetCallerIdentity",
            "Version": "2011-06-15",
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"SENTINEL/20261118/{installed.region}/sts/aws4_request",
            "X-Amz-Date": "20261118T100000Z",
            "X-Amz-Expires": "60",
            "X-Amz-Security-Token": "sentinel-token",
            "X-Amz-Signature": "b" * 64,
            "X-Amz-SignedHeaders": "host;x-opentour-enrollment",
        }
    )
    return {
        "transcript": transcript,
        "public_jwk": jwk,
        "signed_sts_request": {
            "method": "GET",
            "url": f"https://sts.{installed.region}.amazonaws.com/?{query}",
            "binding_header": b64(hashlib.sha256(canonical).digest()),
        },
        "runner_signature": b64(r.to_bytes(32) + s.to_bytes(32)),
    }


def assert_denied(body: bytes, installed: InstalledCell, challenge: IssuedChallenge) -> None:
    with pytest.raises(InvalidEnrollmentProof, match="^ENROLLMENT_PROOF_INVALID$"):
        validate_enrollment_proof(body, installed=installed, challenge=challenge, now=NOW)


@pytest.mark.parametrize("mode", ["opentour_managed", "customer_controlled"])
@pytest.mark.parametrize("role_id", [None, "AROAEXISTINGROLE"])
def test_preflight_accepts_exact_approved_installation(installed, challenge, mode, role_id):
    installed = replace(installed, deployment_mode=mode, expected_iam_role_id=role_id)
    challenge = replace(challenge, installed=installed)
    validate_enrollment_proof(
        fixture_json(proof_for(installed, challenge)),
        installed=installed,
        challenge=challenge,
        now=NOW,
    )


@pytest.mark.parametrize(
    "name,invalid",
    [
        ("organization_id", "another-org"),
        ("environment_id", "another-env"),
        ("cell_id", "other-cell"),
        ("aws_account_id", "000000000002"),
        ("iam_role_arn", "arn:aws:iam::000000000001:role/other/fixture-role"),
        ("expected_iam_role_id", "AROARECREATEDROLE"),
        ("region", "us-east-2"),
        ("deployment_mode", "customer_controlled"),
        ("installer_sha256", "b" * 64),
        ("challenge_id", "other-challenge"),
        ("nonce", b64(bytes(32))),
        ("runner_jwk_thumbprint", b64(bytes(32))),
        ("protocol", "other"),
    ],
)
def test_each_transcript_input_is_bound(installed, challenge, name, invalid):
    proof = proof_for(installed, challenge)
    proof["transcript"][name] = invalid
    assert_denied(fixture_json(proof), installed, challenge)


@pytest.mark.parametrize("suffix", ["\n", "\r\n"])
@pytest.mark.parametrize("part", ["nonce", "runner_signature"])
def test_enr04_newline_suffix_is_denied(installed, challenge, suffix, part):
    proof = proof_for(installed, challenge)
    field = proof["transcript"] if part == "nonce" else proof
    field[part] += suffix
    assert_denied(fixture_json(proof), installed, challenge)


@pytest.mark.parametrize(
    "old,new",
    [
        ("sts.us-east-1.amazonaws.com", "sts.us-east-2.amazonaws.com"),
        ("sts.us-east-1.amazonaws.com", "sts.us-east-1.amazonaws.com@fixture.example"),
        ("sts.us-east-1.amazonaws.com", "fixture.example@sts.us-east-1.amazonaws.com"),
        ("sts.us-east-1.amazonaws.com", "sts.us-east-1.amazonaws.com:443"),
        ("https://", "http://"),
        ("/?", "/redirect?"),
        ("Action=GetCallerIdentity", "Action=AssumeRole"),
        ("Action=GetCallerIdentity", "Action=GetCallerIdentity&Action=GetCallerIdentity"),
        ("Action=GetCallerIdentity", "Action=GetCallerIdentity&%41ction=GetCallerIdentity"),
        ("Action=GetCallerIdentity", "Action=GetCallerIdentity&RoleArn=sentinel"),
        ("X-Amz-Signature=", "X-Amz-Signature=%%%"),
        ("&X-Amz-Signature=", "#X-Amz-Signature="),
        ("2011-06-15", "2011-06-16"),
        ("X-Amz-Expires=60", "X-Amz-Expires=61"),
        ("X-Amz-Date=20261118T100000Z", "X-Amz-Date=20261118T100001Z"),
        ("host%3Bx-opentour-enrollment", "host"),
        ("&X-Amz-Security-Token=sentinel-token", ""),
        ("us-east-1%2Fsts", "us-east-2%2Fsts"),
    ],
)
def test_enr05_request_cannot_change_target_action_signed_scope_or_expiry(
    installed, challenge, old, new
):
    proof = proof_for(installed, challenge)
    url = proof["signed_sts_request"]["url"]
    assert old in url
    proof["signed_sts_request"]["url"] = url.replace(old, new)
    assert_denied(fixture_json(proof), installed, challenge)


def test_enr02_changing_key_or_signed_binding_is_denied(installed, challenge):
    proof = proof_for(installed, challenge)
    other = ec.derive_private_key(10, ec.SECP256R1()).public_key().public_numbers()
    proof["public_jwk"].update(x=b64(other.x.to_bytes(32)), y=b64(other.y.to_bytes(32)))
    assert_denied(fixture_json(proof), installed, challenge)
    proof = proof_for(installed, challenge)
    proof["signed_sts_request"]["binding_header"] = b64(bytes(32))
    assert_denied(fixture_json(proof), installed, challenge)


def test_rfc8785_transcript_signature_is_verified(installed, challenge):
    proof = proof_for(installed, challenge)
    key = ec.derive_private_key(10, ec.SECP256R1())
    other_signature = key.sign(fixture_json(proof["transcript"]), ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(other_signature)
    proof["runner_signature"] = b64(r.to_bytes(32) + s.to_bytes(32))
    assert_denied(fixture_json(proof), installed, challenge)


def test_approved_challenge_binds_installation_and_limits_aws_expiry(installed, challenge):
    replacement = replace(installed, expected_iam_role_id="AROARECREATEDROLE")
    assert_denied(fixture_json(proof_for(replacement, challenge)), replacement, challenge)
    proof = fixture_json(proof_for(installed, challenge))
    assert_denied(proof, installed, replace(challenge, expires_at=NOW + timedelta(seconds=59)))
    assert_denied(proof, installed, replace(challenge, expires_at=NOW))


def test_unqualified_ohio_profile_cannot_admit_proof(installed, challenge):
    ohio = replace(installed, region="us-east-2")
    challenge = replace(challenge, installed=ohio)
    assert_denied(fixture_json(proof_for(ohio, challenge)), ohio, challenge)


def test_size_and_duplicate_json_fields_are_rejected(installed, challenge):
    proof = fixture_json(proof_for(installed, challenge))
    assert_denied(proof + b" " * (16 * 1024), installed, challenge)
    duplicate = b'{"runner_signature":"sentinel",' + proof[1:]
    assert_denied(duplicate, installed, challenge)


def test_invalid_ec_point_and_private_key_members_are_rejected(installed, challenge):
    proof = proof_for(installed, challenge)
    proof["public_jwk"].update(x=b64(bytes(32)), y=b64(bytes(32)))
    assert_denied(fixture_json(proof), installed, challenge)
    proof = proof_for(installed, challenge)
    proof["public_jwk"]["d"] = b64(bytes(32))
    assert_denied(fixture_json(proof), installed, challenge)


def test_unused_base64_pad_bits_are_rejected(installed, challenge):
    proof = proof_for(installed, challenge)
    canonical_nonce = proof["transcript"]["nonce"]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    noncanonical = canonical_nonce[:-1] + alphabet[alphabet.index(canonical_nonce[-1]) + 1]
    assert base64.urlsafe_b64decode(noncanonical + "=") == NONCE_BYTES
    proof["transcript"]["nonce"] = noncanonical
    assert_denied(fixture_json(proof), installed, challenge)


def test_request_must_not_be_forwarded_at_aws_signature_expiry(installed, challenge):
    with pytest.raises(InvalidEnrollmentProof, match="^ENROLLMENT_PROOF_INVALID$"):
        validate_enrollment_proof(
            fixture_json(proof_for(installed, challenge)),
            installed=installed,
            challenge=challenge,
            now=NOW + timedelta(seconds=60),
        )
