"""Local enrollment proof conformance using only generated data and keys."""

from __future__ import annotations

import base64
import hashlib
import json
import ssl
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any
from urllib.parse import urlencode

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.x509.oid import NameOID
from opentour_control.runners.enrollment import (
    MAX_STS_RESPONSE_BYTES,
    EnrollmentVerificationUnavailable,
    HTTPSSTSIdentityExchange,
    InstalledCell,
    InvalidEnrollmentProof,
    IssuedChallenge,
    STSIdentityRequest,
    STSIdentityResponse,
    validate_enrollment_proof,
    verify_enrollment_identity,
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


AWS_ROLE_ID = "AROAEXISTINGROLE"
AWS_STS_SESSION = "fixture-role-session"
AWS_ASSUMED_ROLE_ARN = f"arn:aws:sts::000000000001:assumed-role/fixture-role/{AWS_STS_SESSION}"


def aws_sts_xml(
    *,
    account: str = "000000000001",
    arn: str = AWS_ASSUMED_ROLE_ARN,
    user_id: str = f"{AWS_ROLE_ID}:{AWS_STS_SESSION}",
) -> bytes:
    return f"""<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
      <GetCallerIdentityResult>
        <Arn>{arn}</Arn><UserId>{user_id}</UserId><Account>{account}</Account>
      </GetCallerIdentityResult>
      <ResponseMetadata><RequestId>01234567-89ab-cdef-0123-456789abcdef</RequestId></ResponseMetadata>
    </GetCallerIdentityResponse>""".encode()


class FixtureSTSExchange:
    def __init__(
        self,
        body: bytes = b"",
        *,
        status: int = 200,
        final_url: str | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.final_url = final_url
        self.failure = failure
        self.calls: list[STSIdentityRequest] = []

    def get_caller_identity(
        self, validated: STSIdentityRequest, *, max_response_bytes: int
    ) -> STSIdentityResponse:
        assert max_response_bytes == MAX_STS_RESPONSE_BYTES
        self.calls.append(validated)
        if self.failure is not None:
            raise self.failure
        return STSIdentityResponse(self.status, self.final_url or validated.url, self.body)


@pytest.mark.parametrize("pinned_role", [None, AWS_ROLE_ID])
def test_sts_verification_binds_role_id_separately_from_session(installed, challenge, pinned_role):
    installed = replace(installed, expected_iam_role_id=pinned_role)
    challenge = replace(challenge, installed=installed)
    proof = proof_for(installed, challenge)
    exchange = FixtureSTSExchange(aws_sts_xml())
    evidence = verify_enrollment_identity(
        fixture_json(proof),
        installed=installed,
        challenge=challenge,
        exchange=exchange,
        clock=lambda: NOW,
    )
    assert len(exchange.calls) == 1
    sent = exchange.calls[0]
    assert sent.url == proof["signed_sts_request"]["url"]
    assert sent.binding_header == proof["signed_sts_request"]["binding_header"]
    assert sent.expires_at == NOW + timedelta(seconds=60)
    assert "sentinel-token" not in repr(sent) and sent.binding_header not in repr(sent)
    assert evidence.challenge_id == challenge.id and evidence.installed is installed
    assert evidence.iam_role_id == AWS_ROLE_ID and AWS_STS_SESSION not in repr(evidence)
    assert evidence.runner_jwk_thumbprint == proof["transcript"]["runner_jwk_thumbprint"]
    assert evidence.expires_at == sent.expires_at
    assert evidence.verified_at == NOW


@pytest.mark.parametrize(
    "observed",
    [
        {"account": "000000000002"},
        {"arn": "arn:aws:iam::000000000001:root"},
        {"arn": "arn:aws:iam::000000000001:user/fixture-role"},
        {"arn": "arn:aws:sts::000000000001:federated-user/fixture-role"},
        {"arn": AWS_ASSUMED_ROLE_ARN.replace("000000000001", "000000000002")},
        {"arn": AWS_ASSUMED_ROLE_ARN.replace("/fixture-role/", "/Fixture-role/")},
        {"arn": AWS_ASSUMED_ROLE_ARN.replace("/fixture-role/", "/cell/fixture-role/")},
        {"user_id": AWS_ROLE_ID},
        {"user_id": AWS_ROLE_ID + ":unrelated-session"},
        {"user_id": AWS_ROLE_ID + ":substitution:" + AWS_STS_SESSION},
        {"user_id": "short:" + AWS_STS_SESSION},
    ],
)
def test_sts_response_must_identify_installed_assumed_role(installed, challenge, observed):
    exchange = FixtureSTSExchange(aws_sts_xml(**observed))
    with pytest.raises(InvalidEnrollmentProof, match="^ENROLLMENT_PROOF_INVALID$"):
        verify_enrollment_identity(
            fixture_json(proof_for(installed, challenge)),
            installed=installed,
            challenge=challenge,
            exchange=exchange,
            clock=lambda: NOW,
        )
    assert len(exchange.calls) == 1


def test_enr01_same_arn_recreated_with_different_role_id_requires_recovery(installed, challenge):
    installed = replace(installed, expected_iam_role_id=AWS_ROLE_ID)
    challenge = replace(challenge, installed=installed)
    exchange = FixtureSTSExchange(aws_sts_xml(user_id=f"AROARECREATEDROLE:{AWS_STS_SESSION}"))
    with pytest.raises(InvalidEnrollmentProof, match="^ENROLLMENT_PROOF_INVALID$"):
        verify_enrollment_identity(
            fixture_json(proof_for(installed, challenge)),
            installed=installed,
            challenge=challenge,
            exchange=exchange,
            clock=lambda: NOW,
        )
    assert len(exchange.calls) == 1


@pytest.mark.parametrize(
    "kind", ["bad-runner-signature", "unqualified-host", "invalid-registered-role"]
)
def test_rejects_unverified_evidence_before_invoking_sts(installed, challenge, kind):
    if kind == "invalid-registered-role":
        installed = replace(installed, iam_role_arn=installed.iam_role_arn + "!")
        challenge = replace(challenge, installed=installed)
    proof = proof_for(installed, challenge)
    if kind == "bad-runner-signature":
        proof["runner_signature"] += "\n"
    if kind == "unqualified-host":
        proof["signed_sts_request"]["url"] = proof["signed_sts_request"]["url"].replace(
            "sts.us-east-1.amazonaws.com", "sts.us-east-2.amazonaws.com"
        )
    exchange = FixtureSTSExchange(aws_sts_xml())
    with pytest.raises(InvalidEnrollmentProof, match="^ENROLLMENT_PROOF_INVALID$"):
        verify_enrollment_identity(
            fixture_json(proof),
            installed=installed,
            challenge=challenge,
            exchange=exchange,
            clock=lambda: NOW,
        )
    assert not exchange.calls


@pytest.mark.parametrize(
    "change",
    [
        {"status": 403},
        {"status": 302},
        {"final_url": "https://example.test"},
        {"body": b" " * (MAX_STS_RESPONSE_BYTES + 1)},
        {"body": b""},
        {"body": aws_sts_xml().replace(b"https://sts.amazonaws.com", b"https://foreign.example")},
        {
            "body": aws_sts_xml().replace(
                b"</Account>", b"</Account><Account>000000000001</Account>"
            )
        },
        {"body": aws_sts_xml().replace(b"</Arn>", b"<Extra/></Arn>")},
        {"body": aws_sts_xml().replace(b"</ResponseMetadata>", b"</WrongTag>")},
        {"body": b"<!DOCTYPE x [<!ENTITY x 'generated'>]>" + aws_sts_xml()},
        {"body": b"<GetCallerIdentityResponse>\xff</GetCallerIdentityResponse>"},
        {"failure": TimeoutError("sentinel-token")},
        {"failure": RuntimeError("sentinel-token")},
    ],
)
def test_refuses_unavailable_malformed_or_redirected_sts_without_retry(
    installed, challenge, change
):
    exchange = FixtureSTSExchange(**({"body": aws_sts_xml()} | change))
    with pytest.raises(
        EnrollmentVerificationUnavailable,
        match="^ENROLLMENT_VERIFICATION_UNAVAILABLE$",
    ):
        verify_enrollment_identity(
            fixture_json(proof_for(installed, challenge)),
            installed=installed,
            challenge=challenge,
            exchange=exchange,
            clock=lambda: NOW,
        )
    assert len(exchange.calls) == 1


@pytest.mark.parametrize("seconds", [-1, 60, 75])
def test_provider_result_expires_while_waiting_for_response(installed, challenge, seconds):
    times = iter((NOW, NOW + timedelta(seconds=seconds)))
    exchange = FixtureSTSExchange(aws_sts_xml())
    with pytest.raises(
        EnrollmentVerificationUnavailable,
        match="^ENROLLMENT_VERIFICATION_UNAVAILABLE$",
    ):
        verify_enrollment_identity(
            fixture_json(proof_for(installed, challenge)),
            installed=installed,
            challenge=challenge,
            exchange=exchange,
            clock=lambda: next(times),
        )
    assert len(exchange.calls) == 1


@pytest.fixture
def approved_wire_request(installed, challenge):
    exchange = FixtureSTSExchange(aws_sts_xml())
    verify_enrollment_identity(
        fixture_json(proof_for(installed, challenge)),
        installed=installed,
        challenge=challenge,
        exchange=exchange,
        clock=lambda: NOW,
    )
    return exchange.calls[0]


@pytest.fixture
def server_trust_pem() -> bytes:
    key = ec.derive_private_key(19, ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture-root")])
    return (
        x509.CertificateBuilder()
        .issuer_name(name)
        .subject_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )


class WireResponse:
    def __init__(self, *, status: int = 200, payload: bytes = b"", headers=None) -> None:
        self.status = status
        self.stream = BytesIO(payload)
        self.headers = headers or {}
        self.read_sizes: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stream.close()

    def getheader(self, name: str, default=None):
        return self.headers.get(name, default)

    def read1(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.stream.read(size)


class WireConnection:
    def __init__(self, response: WireResponse, on_connect: Callable[[], None] = lambda: None):
        self.response = response
        self.on_connect = on_connect
        self.events: list[object] = []
        self.sock = None
        self.closed = threading.Event()

    def set_debuglevel(self, level):
        self.events.append(("debug", level))

    def settimeout(self, seconds: float) -> None:
        assert 0 < seconds <= 60

    def connect(self):
        self.events.append("tls")
        self.on_connect()
        self.sock = self

    def request(self, method, target, *, headers):
        assert self.sock is not None
        self.events.append((method, target, headers))

    def getresponse(self):
        self.events.append("response")
        return self.response

    def close(self):
        self.closed.set()


def use_local_tls_fixture(monkeypatch, connection: WireConnection):
    initialized = []

    def only_local_test_evidence(host, *, port, timeout, context):
        initialized.append((host, port, timeout, context))
        return connection

    monkeypatch.setattr("http.client.HTTPSConnection", only_local_test_evidence)
    return initialized


def test_real_transport_requests_only_exact_presigned_sts_target(
    installed,
    challenge,
    server_trust_pem,
    monkeypatch,
    capsys,
    caplog,
):
    proof = proof_for(installed, challenge)
    wire = WireConnection(WireResponse(payload=aws_sts_xml()))
    calls = use_local_tls_fixture(monkeypatch, wire)
    monkeypatch.setenv("AWS_PROFILE", "unapproved-profile")
    monkeypatch.setenv("HTTPS_PROXY", "https://unapproved-proxy.example")
    monkeypatch.setenv("SSL_CERT_FILE", "/unapproved/ca.pem")
    monkeypatch.setenv("SSLKEYLOGFILE", "/unapproved/keylog")
    transport = HTTPSSTSIdentityExchange(
        region="us-east-1",
        trusted_ca_pem=server_trust_pem,
        timeout_seconds=5,
        clock=lambda: NOW,
    )
    try:
        result = verify_enrollment_identity(
            fixture_json(proof),
            installed=installed,
            challenge=challenge,
            exchange=transport,
            clock=lambda: NOW,
        )
    finally:
        transport.close()
    assert result.iam_role_id == AWS_ROLE_ID
    [(host, port, timeout, tls)] = calls
    assert (host, port) == ("sts.us-east-1.amazonaws.com", 443) and 0 < timeout <= 5
    assert tls.check_hostname and tls.verify_mode == ssl.CERT_REQUIRED
    assert tls.minimum_version == ssl.TLSVersion.TLSv1_2 and tls.keylog_filename is None
    assert tls.cert_store_stats()["x509_ca"] == 1
    request = proof["signed_sts_request"]
    expected_target = request["url"].removeprefix("https://sts.us-east-1.amazonaws.com")
    assert wire.events == [
        ("debug", 0),
        "tls",
        (
            "GET",
            expected_target,
            {
                "x-opentour-enrollment": request["binding_header"],
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        ),
        "response",
    ]
    assert wire.closed.is_set()
    assert "sentinel-token" not in caplog.text and "sentinel-token" not in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["forged", "host-swap", "header-swap", "expiry-swap", "expired"])
def test_transport_denies_unsealed_or_changed_wire_request_without_tls(
    approved_wire_request,
    server_trust_pem,
    monkeypatch,
    kind,
):
    approved = approved_wire_request
    choices = {
        "forged": STSIdentityRequest(approved.url, approved.binding_header, approved.expires_at),
        "host-swap": replace(approved, url=approved.url.replace("us-east-1", "us-east-2")),
        "header-swap": replace(approved, binding_header=b64(bytes(32))),
        "expiry-swap": replace(approved, expires_at=approved.expires_at + timedelta(seconds=10)),
        "expired": approved,
    }
    calls = use_local_tls_fixture(monkeypatch, WireConnection(WireResponse()))
    now = approved.expires_at if kind == "expired" else NOW
    transport = HTTPSSTSIdentityExchange(
        region="us-east-1",
        trusted_ca_pem=server_trust_pem,
        timeout_seconds=5,
        clock=lambda: now,
    )
    try:
        with pytest.raises(
            EnrollmentVerificationUnavailable, match="^ENROLLMENT_VERIFICATION_UNAVAILABLE$"
        ):
            transport.get_caller_identity(choices[kind], max_response_bytes=MAX_STS_RESPONSE_BYTES)
    finally:
        transport.close()
    assert calls == []


@pytest.mark.parametrize(
    "status,headers,body,reads",
    [
        (302, {"Location": "https://unapproved.example"}, b"not read", 0),
        (503, {}, b"not read", 0),
        (200, {"Content-Encoding": "gzip"}, b"not read", 0),
        (200, {"Content-Length": "99"}, b"not read", 0),
        (200, {"Content-Length": "12, 12"}, b"not read", 0),
        (200, {"Content-Length": "12", "Transfer-Encoding": "chunked"}, b"not read", 0),
        (200, {"Transfer-Encoding": "identity"}, b"not read", 0),
        (200, {"Content-Length": "12"}, b"partial", 2),
        (200, {}, b"x" * 33, 1),
    ],
)
def test_transport_refuses_unbounded_or_unexpected_response_without_retry(
    approved_wire_request,
    server_trust_pem,
    monkeypatch,
    status,
    headers,
    body,
    reads,
):
    response = WireResponse(status=status, headers=headers, payload=body)
    wire = WireConnection(response)
    calls = use_local_tls_fixture(monkeypatch, wire)
    transport = HTTPSSTSIdentityExchange(
        region="us-east-1",
        trusted_ca_pem=server_trust_pem,
        timeout_seconds=5,
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(
            EnrollmentVerificationUnavailable, match="^ENROLLMENT_VERIFICATION_UNAVAILABLE$"
        ):
            transport.get_caller_identity(approved_wire_request, max_response_bytes=32)
    finally:
        transport.close()
    assert len(calls) == 1 and wire.closed.is_set()
    assert len(response.read_sizes) == reads
    assert all(requested <= 33 for requested in response.read_sizes)


def test_transport_timeout_never_sends_query_after_slow_tls(
    approved_wire_request,
    server_trust_pem,
    monkeypatch,
):
    connect_started = threading.Event()
    release = threading.Event()

    def delayed_tls():
        connect_started.set()
        assert release.wait(3)

    wire = WireConnection(WireResponse(payload=aws_sts_xml()), on_connect=delayed_tls)
    calls = use_local_tls_fixture(monkeypatch, wire)
    transport = HTTPSSTSIdentityExchange(
        region="us-east-1",
        trusted_ca_pem=server_trust_pem,
        timeout_seconds=0.15,
        clock=lambda: NOW,
    )
    try:
        with pytest.raises(
            EnrollmentVerificationUnavailable, match="^ENROLLMENT_VERIFICATION_UNAVAILABLE$"
        ):
            transport.get_caller_identity(
                approved_wire_request, max_response_bytes=MAX_STS_RESPONSE_BYTES
            )
        assert connect_started.is_set()
    finally:
        release.set()
        transport.close()
    assert wire.closed.wait(3)
    assert len(calls) == 1 and wire.events == [("debug", 0), "tls"]


@pytest.mark.parametrize(
    "change",
    [
        {"region": "us-east-2"},
        {"trusted_ca_pem": b"unapproved-ca"},
        {"timeout_seconds": 61},
        {"timeout_seconds": float("nan")},
    ],
)
def test_unapproved_transport_configuration_fails_closed(server_trust_pem, change):
    options = {"region": "us-east-1", "trusted_ca_pem": server_trust_pem, "timeout_seconds": 5}
    with pytest.raises(
        EnrollmentVerificationUnavailable, match="^ENROLLMENT_VERIFICATION_UNAVAILABLE$"
    ):
        HTTPSSTSIdentityExchange(**(options | change))
